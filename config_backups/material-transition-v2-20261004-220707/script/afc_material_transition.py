"""Temperature-aware AFC loading, unloading and residual-material flushing.

Local Klipper extension. AFC sources remain unchanged. Supported hooks are
checked at connect; an incompatible AFC update fails configuration loading.
"""
import math


def material_key(value):
    return ''.join(c for c in str(value).upper() if c.isalnum())


def transition_plan(old_temp, new_temp, cap, heater_max):
    values = (old_temp, new_temp, cap, heater_max)
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Non-finite material temperature')
    if old_temp < 0 or new_temp < 170:
        raise ValueError('Material temperature must be configured (at least 170C)')
    flush = max(old_temp, new_temp)
    if flush >= heater_max:
        raise ValueError('Transition temperature %.0fC reaches the heater limit' % flush)
    if new_temp > cap or flush > cap:
        raise ValueError(
            'Required flush %.0fC exceeds incoming material transition limit %.0fC. '
            'Use a compatible cleaning/bridge filament or manually clean the hotend '
            'before confirming AFC_TRANSITION_CLEANED.' % (flush, cap))
    return flush


class MaterialTransition:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config_error = config.error
        self.gcode = self.printer.lookup_object('gcode')
        self.flush_length = config.getfloat('flush_length', 80., above=0.)
        self.flush_speed = config.getfloat('flush_speed', 2., above=0.)
        self.load_speed = config.getfloat('load_speed', 5., above=0.)
        self.finish_length = config.getfloat('finish_length', 25., above=0.)
        self.retract = config.getfloat('cooling_retract', 1., minval=0., maxval=3.)
        self.caps = {}
        for pair in config.get('transition_limits', '').split(','):
            if pair.strip():
                name, value = pair.split(':', 1)
                self.caps[material_key(name)] = float(value)
        self.active = None
        self.mode = None
        self.residual = 0.
        self.printer.register_event_handler('klippy:connect', self.connect)
        self.gcode.register_command('AFC_TRANSITION_PURGE', self.purge)
        self.gcode.register_command('AFC_TRANSITION_STATUS', self.status)
        self.gcode.register_command('AFC_TRANSITION_CLEANED', self.cleaned)

    def connect(self):
        self.afc = self.printer.lookup_object('AFC')
        self.toolhead = self.printer.lookup_object('toolhead')
        self.heaters = self.printer.lookup_object('heaters')
        self.saved = self.printer.lookup_object('save_variables')
        self.residual = float(self.saved.allVariables.get('afc_residual_temp', 0.))
        for name in ('TOOL_LOAD', 'TOOL_UNLOAD', 'CHANGE_TOOL',
                     '_check_extruder_temp', '_get_default_material_temps'):
            if not callable(getattr(self.afc, name, None)):
                raise self.config_error('AFC transition hook missing: '+name)
        if not self.afc.poop or self.afc.poop_cmd != 'AFC_POOP':
            raise self.config_error('AFC transition requires poop_cmd: AFC_POOP')
        self.poop = self.printer.lookup_object('gcode_macro _AFC_POOP_VARS')
        self.original_load = self.afc.TOOL_LOAD
        self.original_unload = self.afc.TOOL_UNLOAD
        self.original_change = self.afc.CHANGE_TOOL
        self.original_check = self.afc._check_extruder_temp
        self.afc.TOOL_LOAD = self.load
        self.afc.TOOL_UNLOAD = self.unload
        self.afc.CHANGE_TOOL = self.change
        self.afc._check_extruder_temp = self.check_temperature
        self.afc.poop_cmd = 'AFC_TRANSITION_PURGE'

    def run(self, script):
        self.gcode.run_script_from_command(script)

    def persist(self, temp):
        self.run('SAVE_VARIABLE VARIABLE=afc_residual_temp VALUE=%.3f' % temp)
        self.residual = temp

    def nominal(self, lane, incoming=False):
        temp, fallback = self.afc._get_default_material_temps(lane)
        if fallback:
            raise self.gcode.error('AFC transition: configure material/nozzle temperature '
                                   'in Spoolman for '+lane.name)
        # The first-layer target supplied by the slicer is authoritative at start.
        start = self.printer.lookup_object('gcode_macro PRINT_START', None)
        if incoming and start and start.variables.get('state') == 'ToolLoad':
            temp = float(start.variables['extruder'])
        if not math.isfinite(temp) or temp < 170:
            raise self.gcode.error('AFC transition: invalid nozzle temperature for '+lane.name)
        return temp

    def plan(self, lane):
        new_temp = self.nominal(lane, incoming=True)
        old_temp = self.residual
        current = self.afc.lanes.get(self.afc.current)
        if current is not None:
            old_temp = max(old_temp, self.nominal(current))
        cap = self.caps.get(material_key(lane.material), new_temp)
        heater = self.toolhead.get_extruder().get_heater()
        try:
            flush = transition_plan(old_temp, new_temp, cap, heater.max_temp)
        except ValueError as exc:
            raise self.gcode.error('AFC transition %s -> %s: %s' %
                                   (self.afc.current or 'residue', lane.name, exc))
        return {'old': old_temp, 'new': new_temp, 'flush': flush,
                'cap': cap, 'lane': lane.name, 'purged': False}

    def set_temp(self, temp, wait=True):
        # Unlike AFC's original helper, never skip temperature checks mid-print.
        if wait:
            self.toolhead.wait_moves()
        heater = self.toolhead.get_extruder().get_heater()
        self.heaters.set_temperature(heater, temp, wait=wait)

    def change(self, lane, purge_length=None, restore_pos=True):
        if lane.name != self.afc.current:
            self.plan(lane)  # Reject incompatible pairs BEFORE unloading/cutting.
        result = self.original_change(lane, purge_length, restore_pos)
        if self.afc.error_state or self.afc.current != lane.name:
            raise self.gcode.error('AFC transition: tool change failed; printing must not continue')
        return result

    def unload(self, lane):
        if lane is None:
            return self.original_unload(lane)
        temp = max(self.nominal(lane), self.residual)
        cap = self.caps.get(material_key(lane.material), self.nominal(lane))
        if temp > cap:
            raise self.gcode.error('AFC transition: residual temperature %.0fC exceeds '
                                   'the loaded material limit %.0fC; inspect/clean the hotend'
                                   % (temp, cap))
        heater = self.toolhead.get_extruder().get_heater()
        if temp >= heater.max_temp:
            raise self.gcode.error('AFC transition: unload temperature exceeds heater limit')
        self.persist(temp)  # A failed unload must not erase residual-material history.
        previous = self.mode
        self.mode = ('unload', temp)
        try:
            ok = self.original_unload(lane)
            if not ok:
                raise self.gcode.error('AFC transition: unload failed; printing must not continue')
            return ok
        finally:
            self.mode = previous

    def load(self, lane, purge_length=None):
        if lane is None:
            return self.original_load(lane, purge_length)
        plan = self.plan(lane)
        self.active = plan
        self.mode = ('load', plan['flush'])
        original_speed = lane.extruder_obj.tool_load_speed
        lane.extruder_obj.tool_load_speed = min(original_speed, self.load_speed)
        try:
            self.persist(plan['flush'])
            self.gcode.respond_info('AFC transition: %s flush %.0fC -> print %.0fC' %
                                    (lane.name, plan['flush'], plan['new']))
            ok = self.original_load(lane, purge_length)
            if ok and not self.afc.error_state and plan['purged']:
                self.persist(plan['new'])
            else:
                raise self.gcode.error('AFC transition: load/purge failed; printing must not continue')
            return ok
        finally:
            # Do not leave PLA at a high transition target after a loading failure.
            try:
                self.set_temp(plan['new'], wait=False)
            finally:
                lane.extruder_obj.tool_load_speed = original_speed
                self.active = None
                self.mode = None

    def check_temperature(self, lane):
        if self.mode is None:
            return self.original_check(lane)
        self.set_temp(self.mode[1], wait=True)
        return True

    def blob(self, length):
        self.run('AFC_POOP PURGE_LENGTH=%.3f' % length)
        self.toolhead.wait_moves()

    def purge(self, gcmd):
        plan = self.active
        if plan is None:
            raise gcmd.error('AFC_TRANSITION_PURGE can only run inside TOOL_LOAD')
        requested = gcmd.get_float('PURGE_LENGTH', 0., minval=0.)
        variables = self.poop.variables
        saved_speed = variables['purge_spd']
        saved_minimum = variables['purge_length_minimum']
        fan = self.printer.lookup_object('fan')
        fan_speed = fan.get_status(self.printer.get_reactor().monotonic())['speed']
        self.run('SET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS VARIABLE=purge_spd VALUE=%.3f\n'
                 'SET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS VARIABLE=purge_length_minimum VALUE=0.1'
                 % self.flush_speed)
        try:
            self.set_temp(plan['flush'])
            self.gcode.respond_info('AFC transition: residual flush at %.0fC' % plan['flush'])
            # Bound exposure at the high temperature; extra slicer purge volume
            # is completed at the destination temperature instead.
            self.blob(self.flush_length)
            # Move the new filament back slightly while the hotend cools; keep
            # the head parked at the waste bucket with maximum part cooling.
            if plan['flush'] > plan['new']:
                self.gcode.respond_info('AFC transition: cooling to %.0fC' % plan['new'])
                self.run('SAVE_GCODE_STATE NAME=AFC_TRANSITION_COOL\nM83\n'
                         'G1 E-%.3f F120\nM106 S255' % self.retract)
                self.set_temp(plan['new'])
                self.run('M83\nG1 E%.3f F120\n'
                         'RESTORE_GCODE_STATE NAME=AFC_TRANSITION_COOL' % self.retract)
            else:
                self.set_temp(plan['new'])
            # Finish at the destination temperature before the normal wipe/kick.
            self.gcode.respond_info('AFC transition: final purge at %.0fC' % plan['new'])
            self.blob(max(self.finish_length, requested-self.flush_length))
            plan['purged'] = True
        finally:
            self.run('M106 S%d\nSET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS '
                     'VARIABLE=purge_spd VALUE=%.3f\nSET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS '
                     'VARIABLE=purge_length_minimum VALUE=%.3f'
                     % (round(fan_speed*255), saved_speed, saved_minimum))

    def status(self, gcmd):
        lane_name = gcmd.get('LANE', None)
        self.gcode.respond_info('AFC transition: residual temperature %.0fC; '
                                'flush %.0fmm at %.1fmm/s' %
                                (self.residual, self.flush_length, self.flush_speed))
        if lane_name:
            lane = self.afc.lanes.get(lane_name)
            if lane is None:
                raise gcmd.error('Unknown lane: '+lane_name)
            self.gcode.respond_info('AFC transition plan: '+str(self.plan(lane)))

    def cleaned(self, gcmd):
        if self.afc.current:
            raise gcmd.error('Unload the current lane and physically clean the hotend first')
        if gcmd.get_int('CONFIRM', 0) != 1:
            raise gcmd.error('After physical hotend cleaning: AFC_TRANSITION_CLEANED CONFIRM=1')
        self.persist(0.)
        self.gcode.respond_info('AFC transition: residual-material history cleared')

    def get_status(self, eventtime):
        return {'residual_temp': self.residual, 'active': self.active,
                'flush_length': self.flush_length, 'flush_speed': self.flush_speed}


def load_config(config):
    return MaterialTransition(config)

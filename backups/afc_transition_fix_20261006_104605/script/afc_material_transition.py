"""AFC material transitions v2. Staged, temperature and volume based.

The planner is hardware independent. The adapter wraps the existing AFC hooks;
it does not replace the cutter, buffer, spool drive or waste-bucket geometry.
"""
import json
import math
import shlex

VERSION = '2.0.0'


def key(value):
    return ''.join(c for c in str(value).upper() if c.isalnum())


class PolicyError(ValueError):
    pass


class Policy:
    def __init__(self, data):
        self.data = data
        if data.get('schema') != 2:
            raise PolicyError('Expected profiles schema 2')
        self.machine, self.defaults = data['machine'], data['defaults']
        for field in ('filament_diameter','nozzle_diameter','heater_ceiling'):
            if not math.isfinite(self.machine[field]) or self.machine[field] <= 0:
                raise PolicyError('Invalid machine '+field)
        self.materials = {key(k): v for k, v in data['materials'].items()}
        self.aliases = {key(k): key(v) for k, v in data.get('aliases', {}).items()}
        self.pairs = data.get('pairs', {})
        self.area = math.pi * self.machine['filament_diameter'] ** 2 / 4.
        for name, p in self.materials.items():
            for field in ('print_min', 'print_max', 'flush_min', 'flush_preferred',
                          'transition_max', 'flush_flow', 'load_flow',
                          'sensitive_above', 'exposure_seconds'):
                if not isinstance(p.get(field), (int, float)) or not math.isfinite(p[field]) or p[field] <= 0:
                    raise PolicyError('Invalid %s.%s' % (name, field))
            if not (p['print_min'] <= p['print_max'] <= p['transition_max'] < self.machine['heater_ceiling']):
                raise PolicyError('Invalid temperature limits for '+name)
            if not (p['print_min'] <= p['flush_min'] <= p['flush_preferred'] <= p['transition_max']):
                raise PolicyError('Invalid flush range for '+name)
        for f in ('chunk_volume', 'finish_volume', 'temperature_wait_timeout', 'temperature_tolerance'):
            if not math.isfinite(self.defaults[f]) or self.defaults[f] <= 0:
                raise PolicyError('Invalid '+f)
        if not 0 <= self.defaults['cooling_retract'] <= 2:
            raise PolicyError('Invalid cooling_retract')

    def profile(self, material):
        name = key(material)
        name = self.aliases.get(name, name)
        p = self.materials.get(name)
        if p is None or not p.get('enabled'):
            raise PolicyError('Material %s is unknown or disabled; configure its verified profile' % material)
        return name, p

    def record(self, material, temperature):
        name, p = self.profile(material)
        if not math.isfinite(temperature) or not p['print_min'] <= temperature <= p['print_max']:
            raise PolicyError('%s nozzle %.0fC is outside configured %.0f..%.0fC' %
                              (name, temperature, p['print_min'], p['print_max']))
        return {'material': name, 'temperature': float(temperature)}

    def plan(self, residue, incoming, requested_volume=0.):
        if not math.isfinite(requested_volume) or requested_volume < 0:
            raise PolicyError('Invalid requested purge volume')
        new_name, new = self.profile(incoming['material'])
        self.record(new_name, incoming['temperature'])
        old_profiles, names = [], []
        for r in residue:
            if r['material'] == 'UNKNOWN':
                raise PolicyError('Unknown legacy/failed residue: confirm the actual current material '
                                  'or physically clean the hotend before switching')
            name, p = self.profile(r['material'])
            self.record(name, r['temperature'])
            old_profiles.append(p)
            names.append(name)
        lo = max([new['flush_min']] + [p['flush_min'] for p in old_profiles])
        hi = min([new['transition_max']] + [p['transition_max'] for p in old_profiles])
        if lo > hi:
            raise PolicyError('No common flush range (%gC minimum, %gC maximum). '
                              'Use a verified bridge/cleaning filament or clean the hotend.' % (lo, hi))
        old_name = names[0] if len(set(names)) == 1 and names else 'EMPTY'
        pair = self.pairs.get(old_name+'->'+new_name, {})
        preferred = max([new['flush_preferred']] + [p['flush_preferred'] for p in old_profiles])
        temperature = pair.get('flush_temperature', min(hi, max(lo, preferred)))
        if not lo <= temperature <= hi:
            raise PolicyError('Pair flush temperature is outside common range')
        flow = min([new['flush_flow'], pair.get('flush_flow', new['flush_flow'])]
                   + [p['flush_flow'] for p in old_profiles])
        load_flow = min([new['load_flow']] + [p['load_flow'] for p in old_profiles])
        displace = pair.get('displace_volume', 60. if residue else 0.)
        mix = pair.get('mix_volume', 60. if residue else 45.)
        finish = max(self.defaults['finish_volume'], requested_volume-displace-mix)
        phases = [{'name': 'displace', 'temperature': temperature, 'volume': displace, 'flow': flow},
                  {'name': 'mix', 'temperature': temperature, 'volume': mix, 'flow': flow},
                  {'name': 'finish', 'temperature': incoming['temperature'], 'volume': finish,
                   'flow': new['flush_flow']}]
        for p in phases:
            if (not all(math.isfinite(p[f]) for f in ('temperature', 'volume', 'flow'))
                    or p['volume'] < 0 or p['flow'] <= 0):
                raise PolicyError('Invalid pair purge setting')
        return {'from': names, 'to': new_name, 'incoming': incoming,
                'load_temperature': temperature, 'load_flow': load_flow,
                'phases': phases, 'sensitive_above': max(new['sensitive_above'], incoming['temperature']+5),
                'exposure_seconds': new['exposure_seconds'], 'cap': new['transition_max']}


class Exposure:
    """Conservative cumulative hot-time accounting, sampled between chunks."""
    def __init__(self, threshold, limit):
        self.threshold, self.limit = threshold, limit
        self.last, self.hot, self.seconds = None, False, 0.

    def check(self, now, temperature):
        hot = temperature > self.threshold
        if self.last is not None and (self.hot or hot):
            self.seconds += max(0., now-self.last)
        self.last, self.hot = now, hot
        if self.seconds >= self.limit:
            raise PolicyError('Hot-time budget exceeded: %.1fs above %.0fC (limit %.0fs)' %
                              (self.seconds, self.threshold, self.limit))


class MaterialTransition:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config_error = config.error
        self.gcode = self.printer.lookup_object('gcode')
        try:
            with open(config.get('profiles_file')) as f:
                self.policy = Policy(json.load(f))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise config.error('AFC transition profiles: '+str(exc))
        self.legacy_temp = config.getfloat('legacy_residue_temperature', 265., minval=0.)
        self.active = self.mode = self.exposure = None
        self.expected_temperature = None
        self.state = {'schema': 2, 'stage': 'uninitialized', 'residue': []}
        self.printer.register_event_handler('klippy:connect', self.connect)
        for name, callback in [('AFC_TRANSITION_PURGE', self.purge),
                               ('AFC_TRANSITION_CHECK', self.check_command),
                               ('AFC_TRANSITION_STATUS', self.status),
                               ('AFC_TRANSITION_CLEANED', self.cleaned),
                               ('AFC_TRANSITION_CONFIRM_CURRENT', self.confirm_current)]:
            self.gcode.register_command(name, callback)

    def connect(self):
        self.afc = self.printer.lookup_object('AFC')
        self.toolhead = self.printer.lookup_object('toolhead')
        self.heaters = self.printer.lookup_object('heaters')
        self.reactor = self.printer.get_reactor()
        self.heater = self.toolhead.get_extruder().get_heater()
        saved = self.printer.lookup_object('save_variables').allVariables
        if 'afc_transition_state_v2' in saved:
            state = saved['afc_transition_state_v2']
            if not isinstance(state, dict) or state.get('schema') != 2 or not isinstance(state.get('residue'), list):
                raise self.config_error('Invalid persisted AFC transition state')
            self.state = state
        else:
            # v1 stored only a temperature. Do not silently invent its material.
            temp = float(saved.get('afc_residual_temp', self.legacy_temp))
            self.state = {'schema': 2, 'stage': 'legacy', 'residue':
                          [{'material': 'UNKNOWN', 'temperature': temp}] if temp else []}
        c = self.printer.lookup_object('configfile').get_status(self.reactor.monotonic())['config']['extruder']
        for field in ('filament_diameter', 'nozzle_diameter'):
            if abs(float(c[field])-self.policy.machine[field]) > .001:
                raise self.config_error('AFC transition profile does not match '+field)
        if self.policy.machine['heater_ceiling'] >= self.heater.max_temp:
            raise self.config_error('Transition ceiling must be below configured heater max_temp')
        if not self.afc.poop or self.afc.poop_cmd != 'AFC_POOP':
            raise self.config_error('AFC transition requires the original AFC_POOP hook')
        self.poop_vars = self.printer.lookup_object('gcode_macro _AFC_POOP_VARS')
        for name in ('TOOL_LOAD', 'TOOL_UNLOAD', 'CHANGE_TOOL', '_check_extruder_temp', 'move_e_pos'):
            fn = getattr(self.afc, name, None)
            if not callable(fn):
                raise self.config_error('AFC hook missing: '+name)
        self.original_load, self.original_unload = self.afc.TOOL_LOAD, self.afc.TOOL_UNLOAD
        self.original_change, self.original_check = self.afc.CHANGE_TOOL, self.afc._check_extruder_temp
        self.original_move = self.afc.move_e_pos
        self.afc.TOOL_LOAD, self.afc.TOOL_UNLOAD = self.load, self.unload
        self.afc.CHANGE_TOOL, self.afc._check_extruder_temp = self.change, self.check_temperature
        self.afc.move_e_pos = self.move_e
        self.afc.poop_cmd = 'AFC_TRANSITION_PURGE'

    def run(self, script):
        self.gcode.run_script_from_command(script)

    def fail(self, message):
        raise self.gcode.error('AFC transition: '+str(message))

    def persist(self, stage, residue):
        state = {'schema': 2, 'stage': stage, 'residue': residue}
        self.run('SAVE_VARIABLE VARIABLE=afc_transition_state_v2 VALUE='+shlex.quote(repr(state)))
        self.state = state

    def lane_record(self, lane, incoming=False):
        temp, fallback = self.afc._get_default_material_temps(lane)
        if fallback:
            self.fail('Missing Spoolman nozzle temperature for '+lane.name)
        try:
            # Keep rejecting inconsistent Spoolman metadata. The user-specified
            # working temperatures are explicit overrides for the three active
            # polymer profiles; PRINT_START remains authoritative for a job.
            self.policy.record(lane.material, float(temp))
            _, profile = self.policy.profile(lane.material)
            temp = profile.get('print_temperature', temp)
        except PolicyError as exc:
            self.fail(exc)
        start = self.printer.lookup_object('gcode_macro PRINT_START', None)
        if incoming and start and start.variables.get('state') == 'ToolLoad':
            temp = float(start.variables['extruder'])
        try:
            return self.policy.record(lane.material, float(temp))
        except PolicyError as exc:
            self.fail(exc)

    def residue(self):
        result = list(self.state['residue'])
        current = self.afc.lanes.get(self.afc.current)
        if current is not None:
            record = self.lane_record(current)
            if record not in result:
                result.append(record)
        return result

    def plan(self, lane, length=0.):
        try:
            return self.policy.plan(self.residue(), self.lane_record(lane, incoming=True),
                                    float(length or 0.) * self.policy.area)
        except (PolicyError, ValueError) as exc:
            self.fail(exc)

    def guard(self, feeding=False):
        if not self.active or not self.exposure:
            return
        temp, target = self.heater.get_temp(self.reactor.monotonic())
        try:
            if temp > self.active['cap'] + 3 or target > self.active['cap']:
                raise PolicyError('Incoming material temperature limit exceeded')
            if self.expected_temperature is not None and abs(target-self.expected_temperature) > .01:
                raise PolicyError('Another command changed the transition heater target')
            if feeding and abs(temp-target) > 5:
                raise PolicyError('Nozzle temperature is not ready for filament feeding')
            self.exposure.check(self.reactor.monotonic(), temp)
        except PolicyError as exc:
            self.fail(exc)

    def temperature(self, target, wait=True):
        if not 170 <= target < self.policy.machine['heater_ceiling']:
            self.fail('Temperature outside printer policy')
        if wait:
            self.toolhead.wait_moves()
        self.heaters.set_temperature(self.heater, target, wait=False)
        self.expected_temperature = target
        if not wait:
            return
        started = self.reactor.monotonic()
        def check(now):
            self.guard()
            if now-started >= self.policy.defaults['temperature_wait_timeout']:
                self.fail('Temperature wait timed out at %.0fC' % target)
            temp, actual_target = self.heater.get_temp(now)
            if abs(actual_target-target) > .01:
                self.fail('Another command changed the transition heater target')
            return (abs(temp-target) > self.policy.defaults['temperature_tolerance']
                    or self.heater.check_busy(now))
        self.printer.wait_while(check)

    def change(self, lane, purge_length=None, restore_pos=True):
        if self.state['stage'] in ('legacy','failed','loading','unloading'):
            self.fail('Unconfirmed or interrupted material state; inspect the hotend and '
                      'confirm the actual current material or clean it before printing')
        if lane.name != self.afc.current:
            self.plan(lane, purge_length)  # Preflight BEFORE any cut/unload.
        result = self.original_change(lane, purge_length, restore_pos)
        if self.afc.error_state or self.afc.current != lane.name:
            self.fail('Tool change failed; printing must not continue')
        return result

    def unload(self, lane):
        if lane is None:
            return self.original_unload(lane)
        residues = self.residue()
        try:
            # Validate residue identity even for a standalone manual unload.
            p = self.policy.plan(residues, self.lane_record(lane))
        except PolicyError as exc:
            self.fail(exc)
        target = p['load_temperature']
        _, profile = self.policy.profile(lane.material)
        if target > profile['transition_max']:
            self.fail('Unsafe unload temperature')
        self.persist('unloading', residues)
        previous = self.mode
        self.mode = ('unload', target)
        try:
            ok = self.original_unload(lane)
            if not ok:
                self.fail('Unload failed; printing must not continue')
            self.persist('cut_residue', residues)
            return ok
        finally:
            self.mode = previous

    def load(self, lane, purge_length=None):
        if lane is None:
            return self.original_load(lane, purge_length)
        plan = self.plan(lane, purge_length)
        self.active = plan
        self.mode = ('load', plan['load_temperature'])
        self.exposure = None
        residues = self.residue()
        if plan['incoming'] not in residues:
            residues.append(plan['incoming'])
        old_speed = lane.extruder_obj.tool_load_speed
        lane.extruder_obj.tool_load_speed = min(old_speed, plan['load_flow']/self.policy.area)
        try:
            self.persist('loading', residues)
            self.gcode.respond_info('AFC v2: %s -> %s, load %.0fC' %
                                    (plan['from'], plan['to'], plan['load_temperature']))
            ok = self.original_load(lane, purge_length)
            if not ok or self.afc.error_state or not plan.get('purged'):
                self.fail('Load/purge failed; printing must not continue')
            self.persist('ready', [plan['incoming']])
            return ok
        except Exception:
            # Retain both materials after an interrupted purge. No further feed.
            self.afc.error_state = True
            self.persist('failed', residues)
            raise
        finally:
            try:
                self.heaters.set_temperature(self.heater, plan['incoming']['temperature'], wait=False)
            finally:
                lane.extruder_obj.tool_load_speed = old_speed
                self.active = self.mode = self.exposure = None
                self.expected_temperature = None

    def check_temperature(self, lane):
        if self.mode is None:
            return self.original_check(lane)
        self.temperature(self.mode[1])
        return True

    def move_e(self, amount, speed, log_string='', wait_tool=False):
        if not self.active or self.mode[0] != 'load' or amount <= 0:
            return self.original_move(amount, speed, log_string, wait_tool)
        if self.exposure is None:
            self.exposure = Exposure(self.active['sensitive_above'], self.active['exposure_seconds'])
        speed = min(speed, self.active['load_flow']/self.policy.area)
        remaining = amount
        while remaining > .000001:
            self.guard(feeding=True)
            distance = min(remaining, self.policy.defaults['chunk_volume']/self.policy.area, speed*2.)
            self.original_move(distance, speed, log_string, True)
            self.guard(feeding=True)
            remaining -= distance

    def blob(self, volume, flow):
        self.guard(feeding=True)
        self.run('SET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS VARIABLE=purge_spd VALUE=%.5f\n'
                 'AFC_V2_BLOB PURGE_LENGTH=%.5f CHUNK_LENGTH=%.5f'
                 % (flow/self.policy.area, volume/self.policy.area,
                    min(self.policy.defaults['chunk_volume'],flow*2.)/self.policy.area))
        self.toolhead.wait_moves()
        self.guard(feeding=True)

    def check_command(self, gcmd):
        if not self.active:
            self.fail('Guarded blob must run inside TOOL_LOAD')
        self.guard(feeding=True)

    def purge(self, gcmd):
        if self.active is None:
            self.fail('AFC_TRANSITION_PURGE can only run inside TOOL_LOAD')
        if self.exposure is None:
            self.exposure = Exposure(self.active['sensitive_above'], self.active['exposure_seconds'])
        # The adapter keeps the existing bucket, servo and Z geometry.
        v = self.poop_vars.variables
        fields = ('purge_spd', 'purge_length_minimum', 'purge_cool_time')
        saved = {f:v[f] for f in fields}
        fan = self.printer.lookup_object('fan').get_status(self.reactor.monotonic())['speed']
        self.run('SET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS VARIABLE=purge_length_minimum VALUE=0.01\n'
                 'SET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS VARIABLE=purge_cool_time VALUE=0')
        try:
            for phase in self.active['phases']:
                if not phase['volume']:
                    continue
                target = phase['temperature']
                current_target = self.heater.get_temp(self.reactor.monotonic())[1]
                retract = self.policy.defaults['cooling_retract'] if target < current_target else 0.
                self.gcode.respond_info('AFC v2 %s: %.0fC, %.0fmm3 at %.1fmm3/s' %
                                        (phase['name'],target,phase['volume'],phase['flow']))
                if retract:
                    self.run('SAVE_GCODE_STATE NAME=AFC_V2_COOL\nM83\nG1 E-%.3f F120\nM106 S255' % retract)
                self.temperature(target)
                if retract:
                    self.run('M83\nG1 E%.3f F120\nRESTORE_GCODE_STATE NAME=AFC_V2_COOL' % retract)
                self.blob(phase['volume'],phase['flow'])
            self.active['purged'] = True
        finally:
            self.run('M106 S%d' % round(fan*255))
            for field,value in saved.items():
                self.run('SET_GCODE_VARIABLE MACRO=_AFC_POOP_VARS VARIABLE=%s VALUE=%s' % (field,value))

    def status(self, gcmd):
        self.gcode.respond_info('AFC transition v%s: %s' % (VERSION,self.state))
        lane = gcmd.get('LANE',None)
        if lane:
            if lane not in self.afc.lanes:
                self.fail('Unknown lane '+lane)
            self.gcode.respond_info('AFC v2 plan: '+json.dumps(self.plan(self.afc.lanes[lane]),ensure_ascii=False))

    def idle_confirmation(self, gcmd):
        stats = self.printer.lookup_object('print_stats').get_status(self.reactor.monotonic())
        if stats['state'] in ('printing','paused') or self.active:
            self.fail('Confirmation is unavailable during printing/transition')
        if gcmd.get_int('CONFIRM',0) != 1:
            self.fail('Physical inspection is required; use CONFIRM=1 afterwards')

    def cleaned(self, gcmd):
        self.idle_confirmation(gcmd)
        if self.afc.current:
            self.fail('Unload the current lane and physically clean the hotend first')
        self.persist('clean',[])

    def confirm_current(self, gcmd):
        self.idle_confirmation(gcmd)
        lane = self.afc.lanes.get(self.afc.current)
        if lane is None:
            self.fail('No loaded lane; use AFC_TRANSITION_CLEANED after physical cleaning')
        self.persist('ready',[self.lane_record(lane)])

    def get_status(self, eventtime):
        return {'version':VERSION,'state':self.state,'active':self.active,
                'hot_seconds':self.exposure.seconds if self.exposure else 0.}


def load_config(config):
    return MaterialTransition(config)

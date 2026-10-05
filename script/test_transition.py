import unittest
from types import SimpleNamespace as NS
from afc_material_transition import MaterialTransition, transition_plan


class Command:
    def __init__(self, **kwargs):
        self.values = kwargs
    error = staticmethod(RuntimeError)
    def get_float(self, key, default=0., **kwargs):
        return float(self.values.get(key, default))


class TransitionTests(unittest.TestCase):
    def make(self, old=265., new=210., material='PLA', residual=0.):
        ext = object.__new__(MaterialTransition)
        events = []
        heater = NS(max_temp=350.)
        ext.toolhead = NS(wait_moves=lambda: events.append(('wait_moves',)),
                          get_extruder=lambda: NS(get_heater=lambda: heater))
        ext.heaters = NS(set_temperature=lambda h, t, wait:
                         events.append(('heat', t, wait)))
        old_lane = NS(name='lane1', material='ASA', extruder_temp=old,
                      extruder_obj=NS(tool_load_speed=25.))
        new_lane = NS(name='lane2', material=material, extruder_temp=new,
                      extruder_obj=NS(tool_load_speed=25.))
        ext.afc = NS(current='lane1', lanes={'lane1': old_lane, 'lane2': new_lane},
                     error_state=False,
                     _get_default_material_temps=lambda lane: (lane.extruder_temp, False))
        ext.caps = {'PLA':270., 'ASA':310.}
        ext.residual = residual
        ext.active = ext.mode = None
        ext.flush_length, ext.flush_speed, ext.load_speed = 80., 2., 5.
        ext.finish_length, ext.retract = 25., 1.
        ext.poop = NS(variables={'purge_spd':6.5, 'purge_length_minimum':60.999})
        fan = NS(get_status=lambda t: {'speed':.35})
        ext.printer = NS(lookup_object=lambda key, default=None:
                         fan if key=='fan' else default,
                         get_reactor=lambda: NS(monotonic=lambda: 0.))
        def run(script):
            events.append(('script',script))
            if script.startswith('SAVE_VARIABLE'):
                return
            for line in script.splitlines():
                if line.startswith('SET_GCODE_VARIABLE'):
                    bits = dict(x.split('=',1) for x in line.split()[1:])
                    ext.poop.variables[bits['VARIABLE']] = float(bits['VALUE'])
        ext.gcode = NS(error=RuntimeError, respond_info=lambda msg: events.append(('info',msg)))
        ext.run = run
        def original_unload(lane):
            ext.check_temperature(lane)
            events.append(('unload',lane.name))
            ext.afc.current=None
            return True
        def original_load(lane, length):
            ext.check_temperature(lane)
            events.append(('feed', lane.name, lane.extruder_obj.tool_load_speed))
            ext.afc.current=lane.name
            ext.purge(Command(PURGE_LENGTH=length or 0.))
            return True
        def original_change(lane, length, restore):
            if ext.afc.current:
                ext.unload(ext.afc.lanes[ext.afc.current])
            return ext.load(lane,length)
        ext.original_unload, ext.original_load = original_unload, original_load
        ext.original_change = original_change
        ext.original_check = lambda lane: None
        return ext, events, new_lane

    def test_downward_flush_before_cooling_and_finish(self):
        ext, events, lane = self.make()
        ext.change(lane)
        blobs=[(i,e[1]) for i,e in enumerate(events) if e[0]=='script' and e[1].startswith('AFC_POOP')]
        cool=next(i for i,e in enumerate(events) if e==('heat',210.,True))
        self.assertEqual([v for _,v in blobs], ['AFC_POOP PURGE_LENGTH=80.000','AFC_POOP PURGE_LENGTH=25.000'])
        self.assertLess(blobs[0][0],cool)
        self.assertLess(cool,blobs[1][0])
        self.assertIn(('feed','lane2',5.),events)
        self.assertEqual(ext.residual,210.)
        self.assertEqual(lane.extruder_obj.tool_load_speed,25.)
        self.assertEqual(ext.poop.variables,{'purge_spd':6.5,'purge_length_minimum':60.999})

    def test_upward_wait_before_feed_even_during_print(self):
        ext, events, lane = self.make(old=220.,new=310.,material='PA66GF')
        ext.change(lane)
        heat=events.index(('heat',310.,True))
        feed=events.index(('feed','lane2',5.))
        self.assertLess(heat,feed)
        self.assertEqual(ext.residual,310.)

    def test_extreme_downward_rejected_before_unload(self):
        ext, events, lane = self.make(old=310.)
        with self.assertRaisesRegex(RuntimeError,'exceeds incoming'):
            ext.change(lane)
        self.assertFalse(events)
        self.assertEqual(ext.afc.current,'lane1')

    def test_manual_unload_residue_survives_and_blocks_future_load(self):
        ext, events, lane = self.make(old=310.)
        ext.unload(ext.afc.lanes['lane1'])
        self.assertEqual(ext.residual,310.)
        with self.assertRaisesRegex(RuntimeError,'exceeds incoming'):
            ext.load(lane)

    def test_failed_purge_preserves_residue_and_restores_speed_target(self):
        ext, events, lane = self.make()
        def fail(length):
            raise RuntimeError('feed failure')
        ext.blob=fail
        with self.assertRaisesRegex(RuntimeError,'feed failure'):
            ext.change(lane)
        self.assertEqual(ext.residual,265.)
        self.assertEqual(lane.extruder_obj.tool_load_speed,25.)
        self.assertEqual(ext.poop.variables['purge_spd'],6.5)
        self.assertIn(('heat',210.,False),events)
        self.assertIsNone(ext.active)

    def test_failed_unload_stops_print(self):
        ext, events, lane = self.make()
        ext.original_unload=lambda lane: False
        with self.assertRaisesRegex(RuntimeError,'unload failed'):
            ext.change(lane)
        self.assertFalse(any(e[0]=='feed' for e in events))

    def test_large_requested_purge_does_not_extend_high_temperature_phase(self):
        ext, events, lane = self.make()
        ext.change(lane,500.)
        blobs=[e[1] for e in events if e[0]=='script' and e[1].startswith('AFC_POOP')]
        self.assertEqual(blobs,['AFC_POOP PURGE_LENGTH=80.000','AFC_POOP PURGE_LENGTH=420.000'])

    def test_first_layer_target_overrides_spool_temperature(self):
        ext, events, lane = self.make()
        original=ext.printer.lookup_object
        ext.printer.lookup_object=lambda name,default=None: (
            NS(variables={'state':'ToolLoad','extruder':220.})
            if name=='gcode_macro PRINT_START' else original(name,default))
        self.assertEqual(ext.plan(lane)['new'],220.)

    def test_unknown_material_no_guessed_high_temperature_allowance(self):
        ext, events, lane = self.make(old=265.,new=210.,material='mystery')
        with self.assertRaisesRegex(RuntimeError,'exceeds incoming'):
            ext.change(lane)

    def test_missing_spool_temperature_rejected(self):
        ext, events, lane = self.make()
        ext.afc._get_default_material_temps=lambda lane: (65.,True)
        with self.assertRaisesRegex(RuntimeError,'configure material'):
            ext.change(lane)

    def test_invalid_temperatures_and_heater_limit(self):
        for values in [(310.,210.,270.,350.),(220.,350.,350.,350.),
                       (0.,65.,270.,350.),(float('nan'),220.,270.,350.)]:
            with self.assertRaises(ValueError):
                transition_plan(*values)


if __name__=='__main__':
    unittest.main(verbosity=2)

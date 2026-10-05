import ast
import copy
import json
import math
from pathlib import Path
import shlex
import unittest
from types import SimpleNamespace as NS
from afc_material_transition import Policy, PolicyError, Exposure, MaterialTransition
from activate import require_idle

DATA = json.loads(Path(__file__).with_name('profiles.json').read_text())


class PolicyTests(unittest.TestCase):
    def setUp(self): self.p = Policy(copy.deepcopy(DATA))
    def r(self, mat, t): return self.p.record(mat,t)

    def test_current_pairs(self):
        for old,new,old_t,new_t in [('ASA','PLA',265,220),('ABS','PLA',265,220),
                                   ('PLA','ASA',220,265),('PLA','ABS',220,265)]:
            p=self.p.plan([self.r(old,old_t)],self.r(new,new_t))
            self.assertEqual(p['load_temperature'],250)
            self.assertEqual([x['temperature'] for x in p['phases']],[250,250,new_t])
            self.assertEqual(sum(x['volume'] for x in p['phases']),180)

    def test_limits_both_directions(self):
        self.p.materials['PA66GF']['enabled']=True
        for old,new,ot,nt in [('PA66GF','PLA',310,220),('PLA','PA66GF',220,310)]:
            with self.assertRaisesRegex(PolicyError,'No common flush'):
                self.p.plan([self.r(old,ot)],self.r(new,nt))

    def test_unknown_and_disabled_materials(self):
        for mat in ['PA66-GF','PETG','unknown','PA']:
            with self.assertRaises(PolicyError):self.r(mat,265)

    def test_legacy_state_is_not_assumed_empty(self):
        with self.assertRaisesRegex(PolicyError,'Unknown legacy'):
            self.p.plan([{'material':'UNKNOWN','temperature':265}],self.r('PLA',220))

    def test_invalid_nominal_temperatures(self):
        for t in [60,265,float('nan'),float('inf')]:
            with self.assertRaises(PolicyError):self.r('PLA',t)

    def test_larger_slicer_purge_adds_to_finish_only(self):
        p=self.p.plan([self.r('ASA',265)],self.r('PLA',220),500)
        self.assertEqual([x['volume'] for x in p['phases']],[60,60,380])

    def test_volume_conversion(self):
        self.assertAlmostEqual(self.p.area,2.4052818754)
        self.assertAlmostEqual(180/self.p.area,74.835303,places=5)

    def test_pair_cannot_override_safe_temperature_band(self):
        self.p.pairs['ASA->PLA']['flush_temperature']=265
        with self.assertRaises(PolicyError):self.p.plan([self.r('ASA',265)],self.r('PLA',220))

    def test_empty_hotend_does_not_need_old_material_displacement(self):
        p=self.p.plan([],self.r('PLA',220))
        self.assertEqual(p['phases'][0]['volume'],0)

    def test_invalid_profile_rejected(self):
        d=copy.deepcopy(DATA);d['materials']['PLA']['flush_flow']=float('nan')
        with self.assertRaises(PolicyError):Policy(d)

    def test_exposure_counts_cumulative_high_time(self):
        e=Exposure(235,10)
        e.check(0,250);e.check(4,250);e.check(5,220);e.check(20,220)
        self.assertEqual(e.seconds,5)
        e.check(21,250)
        with self.assertRaises(PolicyError):e.check(26,250)


class RuntimeTests(unittest.TestCase):
    def make(self):
        x=object.__new__(MaterialTransition)
        x.policy=Policy(copy.deepcopy(DATA))
        x.state={'schema':2,'stage':'ready','residue':[x.policy.record('ASA',265)]}
        x.active=x.mode=x.exposure=x.expected_temperature=None
        events=[]; clock=NS(now=0.)
        heater=NS(temp=265.,target=265.,max_temp=350.)
        heater.get_temp=lambda now:(heater.temp,heater.target)
        heater.check_busy=lambda now:abs(heater.temp-heater.target)>1
        x.heater=heater
        x.reactor=NS(monotonic=lambda:clock.now)
        x.toolhead=NS(wait_moves=lambda:None)
        def set_temp(h,temp,wait=False):
            heater.target=temp;events.append(('target',temp))
        x.heaters=NS(set_temperature=set_temp)
        def wait_while(check):
            for _ in range(1000):
                if not check(clock.now):return
                clock.now+=.25
                heater.temp += max(-2.5,min(2.5,heater.target-heater.temp))
            raise RuntimeError('wait fixture timed out')
        lane1=NS(name='lane1',material='ASA',extruder_temp=265.,extruder_obj=NS(tool_load_speed=25.))
        lane2=NS(name='lane2',material='PLA',extruder_temp=210.,extruder_obj=NS(tool_load_speed=25.))
        x.afc=NS(current='lane1',lanes={'lane1':lane1,'lane2':lane2},error_state=False,
                 _get_default_material_temps=lambda lane:(lane.extruder_temp,False))
        x.poop_vars=NS(variables={'purge_spd':6.5,'purge_length_minimum':60.999,'purge_cool_time':2})
        fan=NS(get_status=lambda now:{'speed':.35})
        start=NS(variables={'state':'Prepare','extruder':220})
        x.printer=NS(lookup_object=lambda name,default=None:
                     {'fan':fan,'gcode_macro PRINT_START':start}.get(name,default),wait_while=wait_while)
        x.gcode=NS(error=RuntimeError,respond_info=lambda s:None)
        def run(script):
            for line in script.splitlines():
                words=shlex.split(line);events.append(('script',line))
                args=dict(word.split('=',1) for word in words[1:] if '=' in word)
                if words[0]=='SAVE_VARIABLE': ast.literal_eval(args['VALUE'])
                if words[0]=='SET_GCODE_VARIABLE':x.poop_vars.variables[args['VARIABLE']]=float(args['VALUE'])
                if words[0]=='AFC_V2_BLOB':
                    amount=float(args['PURGE_LENGTH']);length=float(args['CHUNK_LENGTH'])
                    speed=x.poop_vars.variables['purge_spd']
                    events.append(('blob',heater.target,amount*x.policy.area,speed*x.policy.area))
                    while amount>1e-6:
                        x.guard(feeding=True);move=min(amount,length)
                        clock.now+=move/speed;amount-=move;x.guard(feeding=True)
        x.run=run
        def original_move(amount,speed,label,wait):
            events.append(('feed',amount,speed,heater.temp));clock.now+=abs(amount)/speed
        x.original_move=original_move
        def original_unload(lane):
            x.check_temperature(lane);events.append(('unload',lane.name));x.afc.current=None;return True
        def original_load(lane,length):
            x.check_temperature(lane);x.move_e(90,lane.extruder_obj.tool_load_speed,'tool stn')
            x.afc.current=lane.name;x.purge(NS());return True
        def original_change(lane,length,restore):
            if x.afc.current:x.unload(x.afc.lanes[x.afc.current])
            return x.load(lane,length)
        x.original_unload,x.original_load,x.original_change=original_unload,original_load,original_change
        x.original_check=lambda lane:None
        return x,lane2,events,heater,clock,start

    def test_full_asa_to_pla_sequence(self):
        x,lane,events,h,clock,start=self.make();x.change(lane)
        blobs=[e for e in events if e[0]=='blob']
        self.assertEqual([e[1] for e in blobs],[250,250,220])
        self.assertAlmostEqual(sum(e[2] for e in blobs),180,places=3)
        self.assertEqual(h.target,220)
        self.assertEqual(x.state['stage'],'ready')
        self.assertEqual(x.state['residue'][0]['material'],'PLA')
        self.assertEqual(lane.extruder_obj.tool_load_speed,25.)
        self.assertEqual(x.poop_vars.variables,{'purge_spd':6.5,'purge_length_minimum':60.999,'purge_cool_time':2})
        feeds=[e for e in events if e[0]=='feed']
        self.assertTrue(all(e[1]*x.policy.area<=12.00001 for e in feeds))
        self.assertAlmostEqual(sum(e[1] for e in feeds),90)

    def test_timeout_stops_feed_and_preserves_both_materials(self):
        x,lane,events,h,clock,start=self.make()
        x.policy.materials['PLA']['exposure_seconds']=5
        with self.assertRaisesRegex(RuntimeError,'Hot-time budget'):x.change(lane)
        self.assertFalse(any(e[0]=='blob' for e in events))
        self.assertEqual(x.state['stage'],'failed')
        self.assertEqual({r['material'] for r in x.state['residue']},{'ASA','PLA'})
        self.assertEqual(h.target,220)
        self.assertEqual(lane.extruder_obj.tool_load_speed,25)
        self.assertTrue(x.afc.error_state)
        with self.assertRaisesRegex(RuntimeError,'interrupted'):x.change(lane)

    def test_preflight_rejects_before_unload_or_temperature_change(self):
        x,lane,events,h,clock,start=self.make()
        x.state['residue']=[{'material':'UNKNOWN','temperature':265}]
        with self.assertRaisesRegex(RuntimeError,'Unknown legacy'):x.change(lane)
        self.assertFalse(events)

    def test_job_temperature_is_authoritative(self):
        x,lane,events,h,clock,start=self.make()
        start.variables={'state':'ToolLoad','extruder':225}
        self.assertEqual(x.plan(lane)['incoming']['temperature'],225)

    def test_heater_target_interference_rejected(self):
        x,lane,events,h,clock,start=self.make()
        x.active=x.plan(lane);x.exposure=Exposure(235,75);x.expected_temperature=250
        h.target=220;h.temp=250
        with self.assertRaisesRegex(RuntimeError,'Another command'):x.guard()

    def test_underheated_feed_rejected(self):
        x,lane,events,h,clock,start=self.make()
        x.active=x.plan(lane);x.exposure=Exposure(235,75);x.expected_temperature=250
        h.target=250;h.temp=220
        with self.assertRaisesRegex(RuntimeError,'not ready'):x.guard(feeding=True)

    def test_failed_blob_restores_macro_settings(self):
        x,lane,events,h,clock,start=self.make()
        def fail_blob(volume,flow):raise RuntimeError('blob failed')
        x.blob=fail_blob
        with self.assertRaisesRegex(RuntimeError,'blob failed'):x.change(lane)
        self.assertEqual(x.poop_vars.variables,{'purge_spd':6.5,'purge_length_minimum':60.999,'purge_cool_time':2})
        self.assertEqual(h.target,220)
        self.assertEqual(x.state['stage'],'failed')


class ActivationTests(unittest.TestCase):
    def status(self):
        return {'webhooks':{'state':'ready'},'print_stats':{'state':'standby'},
                'pause_resume':{'is_paused':False},'virtual_sdcard':{'is_active':False},
                'gcode_macro PRINT_START':{'state':'Prepare'}}
    def test_active_print_refused(self):
        s=self.status();s['print_stats']['state']='printing'
        with self.assertRaises(RuntimeError):require_idle(s)
    def test_pause_refused(self):
        s=self.status();s['pause_resume']['is_paused']=True
        with self.assertRaises(RuntimeError):require_idle(s)
    def test_preparation_refused(self):
        s=self.status();s['gcode_macro PRINT_START']['state']='ToolLoad'
        with self.assertRaises(RuntimeError):require_idle(s)
    def test_idle_allowed(self):require_idle(self.status())
    def test_error_allowed_only_for_rollback(self):
        s={'webhooks':{'state':'error'}}
        with self.assertRaises(RuntimeError):require_idle(s)
        require_idle(s,allow_error=True)


if __name__=='__main__':unittest.main(verbosity=2)

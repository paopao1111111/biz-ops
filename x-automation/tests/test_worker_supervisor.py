import importlib.util
import json
import queue
import tempfile
import time
import unittest
import sys
import os
from pathlib import Path

WORKER_PATH = Path('/private/tmp/x-browse-v2-staging/worker/worker.py')
spec = importlib.util.spec_from_file_location('staged_worker', WORKER_PATH)
w = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = w
spec.loader.exec_module(w)


def config(**changes):
    values = dict(controller_url='http://controller', worker_id='w', worker_secret='s',
                  adspower_base_url='http://ads', adspower_api_key='', poll_seconds=1,
                  heartbeat_seconds=10, request_timeout_seconds=5, progress_seconds=20,
                  max_concurrent_jobs=3, log_file='x.log', no_forward_progress_seconds=30,
                  hard_runtime_grace_seconds=5, probe_hard_runtime_seconds=60,
                  cooperative_cancel_seconds=1, terminate_grace_seconds=1,
                  cleanup_timeout_seconds=10, reconciliation_journal='state/reconciliation.json')
    values.update(changes)
    return w.Config(**values)


def job(jid=1, profile=None, port=None, kind='browse', reserved=10):
    snapshot = {'read_only': True, 'job_type': kind, 'keywords': [], 'trending_target': 0,
                'dwell_seconds': [20, 20], 'max_failures': 1}
    return {'id': jid, 'run_id': jid + 100, 'account_id': jid + 200,
            'profile_id': profile or f'p{jid}', 'proxy_port': port or 10000 + jid,
            'job_type': kind, 'reserved_seconds': reserved, 'expected_handle': '',
            'execution_token': f't{jid}', 'config_snapshot': json.dumps(snapshot)}


class FakeController:
    def __init__(self, complete_failures=0):
        self.calls = []; self.complete_failures = complete_failures; self.directives = []
    def request(self, method, path, data=None, execution_token=None):
        self.calls.append((method, path, data, execution_token))
        if path.endswith('/complete') and self.complete_failures:
            self.complete_failures -= 1
            raise w.WorkerError('offline')
        if path.endswith('/progress'): return {'ok': True, 'accepted': True, 'directive': 'continue'}
        if path.endswith('/complete'): return {'ok': True, 'directive': 'forget'}
        return {'ok': True}
    def heartbeat(self, payload):
        self.calls.append(('POST', '/api/worker/heartbeat', payload, None))
        return {'ok': True, 'directives': self.directives}


class FakeAds:
    def __init__(self, active=False, stop_failures=0, start_error=None, active_states=None):
        self.active = active; self.active_states=list(active_states or []); self.start_calls = 0; self.stop_calls = 0; self.stop_failures = stop_failures; self.start_error=start_error
    def active_state(self, profile): return self.active_states.pop(0) if self.active_states else self.active
    def start(self, profile):
        self.start_calls += 1
        if self.start_error: raise self.start_error
        return {'webdriver': '/tmp/geckodriver', 'marionette_port': '2828', 'debug_port': '9222'}
    def stop_and_confirm(self, profile, timeout=None):
        self.stop_calls += 1
        if self.stop_failures:
            self.stop_failures -= 1
            raise w.AdsPowerError('still active')


class FakeEvent:
    def __init__(self): self.value = False
    def set(self): self.value = True
    def is_set(self): return self.value


class FakeProcess:
    next_pid = 4000
    def __init__(self, alive=True):
        self.alive = alive; self.terminated = 0; self.killed = 0; self.joined = []
        self.pid = FakeProcess.next_pid; FakeProcess.next_pid += 1
    def is_alive(self): return self.alive
    def join(self, seconds=None): self.joined.append(seconds)
    def terminate(self): self.terminated += 1
    def kill(self): self.killed += 1; self.alive = False


class FakeMP:
    def Queue(self): return queue.Queue()
    def Event(self): return FakeEvent()
    def Process(self, **kwargs):
        p = FakeProcess(True); p.target = kwargs['target']; p.args = kwargs['args']; p.name = kwargs['name']; return p


class Addr:
    def __init__(self, port): self.port=port
class Conn:
    def __init__(self, port): self.laddr=Addr(port); self.status='LISTEN'
class Proc:
    def __init__(self, pid, created, exe, cmd, parents=(), ports=(), name=None):
        self.pid=pid; self.created=created; self._exe=exe; self._cmd=cmd; self._parents=parents; self._ports=ports; self._name=name or Path(exe.replace('\\','/')).name
        self.terminated=0; self.killed=0
    def create_time(self): return self.created
    def exe(self): return self._exe
    def name(self): return self._name
    def cmdline(self): return self._cmd
    def parents(self): return self._parents
    def net_connections(self, kind='inet'): return [Conn(p) for p in self._ports]
    def terminate(self): self.terminated += 1
    def kill(self): self.killed += 1
class FakePsutil:
    def __init__(self, procs, alive=False): self.procs=procs; self.waits=[]; self.alive=alive
    def process_iter(self): return list(self.procs)
    def wait_procs(self, procs, timeout): self.waits.append((list(procs),timeout)); return ([],list(procs) if self.alive else [])


class WorkerSupervisorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = []

    def tearDown(self):
        for item in self._tmp:
            item.cleanup()

    def supervisor(self, **kw):
        d=tempfile.TemporaryDirectory(); self._tmp.append(d)
        return w.Supervisor(config(), kw.get('controller', FakeController()), kw.get('ads', FakeAds()), FakeMP(), kw.get('psutil', False), Path(d.name)/'reconciliation.json')

    def test_config_bounds_and_finite(self):
        base = {'controller_url':'http://c','worker_id':'w','worker_secret':'s','adspower_base_url':'http://a'}
        for key, value in [('no_forward_progress_seconds',29),('hard_runtime_grace_seconds',601),
                           ('probe_hard_runtime_seconds',float('inf')),('cooperative_cancel_seconds',0),
                           ('terminate_grace_seconds',31),('cleanup_timeout_seconds',9)]:
            with tempfile.TemporaryDirectory() as d:
                p=Path(d)/'w.json'; p.write_text(json.dumps({**base,key:value}))
                with self.assertRaises(SystemExit): w.Config.load(p)

    def test_heartbeat_payload_and_directives(self):
        s=self.supervisor(); x,_=s.register(job())
        x.process=FakeProcess(); x.state='running'
        payload=s.heartbeat_payload()
        self.assertEqual(payload['protocol_version'], w.PROTOCOL_VERSION)
        self.assertIn('spawn_jobs', payload['capabilities'])
        self.assertEqual(payload['executions'][0]['execution_token'], 't1')
        s.apply_directives([{'job_id':1,'directive':'cancel'}])
        self.assertEqual(x.state,'stopping')

    def test_child_result_ipc(self):
        s=self.supervisor(); x,_=s.register(job()); x.ipc_queue=queue.Queue()
        x.ipc_queue.put({'kind':'forward_progress','milestone':'attached'})
        x.ipc_queue.put({'kind':'snapshot','phase':'search','search_count':2})
        x.ipc_queue.put({'kind':'item','item':{'url':'https://x.com/a/status/1'}})
        x.ipc_queue.put({'kind':'result','result':{'status':'succeeded'}})
        s.drain_ipc(x)
        self.assertEqual((x.phase,x.search_count,len(x.seen),x.result['status']),('search',2,1,'succeeded'))

    def test_watchdogs(self):
        s=self.supervisor(); x,_=s.register(job(reserved=10)); now=time.monotonic()
        x.started_monotonic=now-16; x.last_forward_progress_at=now
        self.assertEqual(s.check_watchdogs(x,now),'hard_runtime_exceeded')
        x.started_monotonic=now; x.last_forward_progress_at=now-31
        self.assertEqual(s.check_watchdogs(x,now),'no_forward_progress')

    def test_terminate_then_kill(self):
        s=self.supervisor(); x,_=s.register(job()); x.process=FakeProcess(True); x.cancel_event=FakeEvent()
        s.terminate_child(x)
        self.assertTrue(x.cancel_event.is_set()); self.assertEqual(x.process.terminated,1); self.assertEqual(x.process.killed,1)

    def test_cleanup_confirmed_and_uncertain(self):
        s=self.supervisor(ads=FakeAds(stop_failures=0)); x,_=s.register(job()); x.owned_launch=True
        self.assertTrue(s.cleanup(x)); self.assertEqual(x.cleanup_state,'confirmed')
        s2=self.supervisor(ads=FakeAds(stop_failures=2)); y,_=s2.register(job(2)); y.owned_launch=True
        self.assertFalse(s2.cleanup(y)); self.assertEqual(y.cleanup_state,'uncertain')

    def test_manual_active_profile_protection(self):
        c=FakeController(); a=FakeAds(active=True); s=self.supervisor(controller=c,ads=a); x,_=s.register(job())
        s.start_registered(x)
        self.assertEqual(a.start_calls,0); self.assertEqual(a.stop_calls,0)
        complete=[call for call in c.calls if call[1].endswith('/complete')][0][2]
        self.assertEqual(complete['failure_detail'],'manual_profile_in_use'); self.assertTrue(complete['cleanup_confirmed'])

    def test_precise_process_discovery_and_descendants(self):
        launch=time.time()-5
        flower=Proc(10,launch+1,r'C:\adspower_global\profile\FlowerBrowser.exe',['FlowerBrowser.exe','--user-data-dir',r'C:\profiles\p1'])
        listener=Proc(11,launch+1,r'C:\other\helper.exe',['helper'],ports=(2828,))
        child=Proc(12,launch+2,r'C:\other\child.exe',['child'],parents=(flower,))
        old=Proc(13,launch-10,r'C:\adspower_global\FlowerBrowser.exe',['FlowerBrowser.exe','p1'])
        same=Proc(14,launch+1,r'C:\adspower_global\FlowerBrowser.exe',['FlowerBrowser.exe','p999'])
        ads=Proc(15,launch+1,r'C:\adspower_global\AdsPower.exe',['AdsPower.exe','p1'],ports=(2828,))
        ps=FakePsutil([flower,listener,child,old,same,ads]); s=self.supervisor(psutil=ps); x,_=s.register(job())
        x.launch_evidence={'profile_id':'p1','launch_started_at':launch,'webdriver':r'C:\drivers\geckodriver.exe','marionette_port':'2828'}
        matched={p.pid for p in s.matched_processes(x)}
        self.assertEqual(matched,{10,11,12}); self.assertNotIn(15,matched)
        s.targeted_cleanup(x)
        self.assertEqual(child.terminated,1); self.assertEqual(flower.terminated,1)

    def test_startup_failures_finalize_and_owned_launch_cleans(self):
        class BadController(FakeController):
            def request(self,*a,**k): raise w.WorkerError('offline')
        s=self.supervisor(controller=BadController()); x,_=s.register(job()); s.start_registered(x)
        self.assertNotIn(1,s.executions); self.assertIn(1,s.reporting)
        class SpawnFailMP(FakeMP):
            def Process(self, **kwargs):
                p=super().Process(**kwargs)
                def fail(): raise OSError('spawn failed')
                p.start=fail; return p
        a=FakeAds(); d=tempfile.TemporaryDirectory(); self._tmp.append(d)
        s2=w.Supervisor(config(),FakeController(complete_failures=1),a,SpawnFailMP(),False,Path(d.name)/'j.json'); y,_=s2.register(job(2)); s2.start_registered(y)
        self.assertTrue(y.owned_launch); self.assertGreaterEqual(a.stop_calls,1); self.assertNotIn(2,s2.executions)

    def test_ambiguous_start_from_definitely_inactive_is_owned_and_cleaned(self):
        a=FakeAds(start_error=w.AdsPowerError('start response failed'),active_states=[False,True])
        s=self.supervisor(ads=a); x,_=s.register(job()); s.start_registered(x)
        self.assertTrue(x.owned_launch); self.assertFalse(x.ownership_uncertain); self.assertGreaterEqual(a.stop_calls,1); self.assertTrue(x.cleanup_confirmed); self.assertEqual(x.completion_payload['failure_code'],'browser_start_failed')

    def test_ambiguous_start_unknown_active_is_quarantined_without_stop(self):
        a=FakeAds(start_error=w.AdsPowerError('start response failed'),active_states=[None,True])
        s=self.supervisor(ads=a); x,_=s.register(job()); s.start_registered(x)
        self.assertFalse(x.owned_launch); self.assertTrue(x.ownership_uncertain); self.assertEqual(a.stop_calls,0); self.assertEqual(x.cleanup_state,'uncertain'); self.assertFalse(x.cleanup_confirmed); self.assertEqual(x.completion_payload['failure_code'],'cleanup_uncertain')

    def test_ambiguous_start_post_state_false_needs_no_cleanup(self):
        a=FakeAds(start_error=w.AdsPowerError('start response failed'),active_states=[None,False])
        s=self.supervisor(ads=a); x,_=s.register(job()); s.start_registered(x)
        self.assertEqual(a.stop_calls,0); self.assertEqual(x.cleanup_state,'not_required'); self.assertTrue(x.cleanup_confirmed); self.assertEqual(x.completion_payload['failure_code'],'browser_start_failed')

    def test_launch_timestamp_precedes_ads_start(self):
        seen=[]
        class Ads(FakeAds):
            def start(self, profile): seen.append(time.time()); return super().start(profile)
        s=self.supervisor(ads=Ads()); x,_=s.register(job()); s.start_registered(x)
        self.assertLessEqual(x.launch_evidence['launch_started_at'],seen[0])

    def test_token_validation(self):
        j=job(); j['execution_token']=''
        with self.assertRaises(w.WorkerError): w.validate_job(j)
        s=self.supervisor(); entry,reason=s.register(j); self.assertIsNone(entry); self.assertIn('execution_token',reason)

    def test_journal_atomic_recovery_and_corruption(self):
        d=tempfile.TemporaryDirectory(); self._tmp.append(d); path=Path(d.name)/'state'/'reconciliation.json'
        c=FakeController(complete_failures=5); s=w.Supervisor(config(),c,FakeAds(),FakeMP(),False,path); x,_=s.register(job()); x.cleanup_confirmed=True; x.cleanup_state='confirmed'; x.result={'status':'failed'}; s.finalize(x)
        self.assertTrue(path.exists()); self.assertNotIn('worker_secret',path.read_text()); self.assertFalse(path.with_name(path.name+'.tmp').exists())
        s2=w.Supervisor(config(),FakeController(),FakeAds(),FakeMP(),False,path); self.assertIn(1,s2.reporting); s2.recover_journal(); self.assertNotIn(1,s2.reporting)
        path.write_text('{bad'); s3=w.Supervisor(config(),FakeController(),FakeAds(),FakeMP(),False,path); self.assertTrue(s3.journal_corrupt); self.assertTrue(s3.draining)

    def test_recovery_without_evidence_is_uncertain_and_locked(self):
        d=tempfile.TemporaryDirectory(); self._tmp.append(d); path=Path(d.name)/'j.json'
        path.write_text(json.dumps({'version':1,'entries':[{'job':job(),'profile_id':'p1','proxy_port':10001,'execution_token':'t1','owned_launch':True,'state':'launching','cleanup_confirmed':False}]}))
        c=FakeController(complete_failures=2); s=w.Supervisor(config(),c,FakeAds(),FakeMP(),False,path); s.recover_journal(); x=s.reporting[1]
        self.assertEqual(x.cleanup_state,'uncertain'); self.assertIn('p1',s.active_profiles); self.assertIn(10001,s.active_proxy_ports)

    def test_prelaunch_recovery_is_not_required_and_released(self):
        for state in ('registered','controller_starting','ownership_check'):
            with self.subTest(state=state):
                d=tempfile.TemporaryDirectory(); self._tmp.append(d); path=Path(d.name)/f'{state}.json'
                path.write_text(json.dumps({'version':1,'entries':[{'job':job(),'profile_id':'p1','proxy_port':10001,'execution_token':'t1','owned_launch':False,'state':state,'cleanup_confirmed':False}]}))
                c=FakeController(complete_failures=2); s=w.Supervisor(config(),c,FakeAds(),FakeMP(),False,path); s.recover_journal(); x=s.reporting[1]
                self.assertEqual(x.cleanup_state,'not_required'); self.assertTrue(x.cleanup_confirmed); self.assertNotIn('p1',s.active_profiles); self.assertNotIn(10001,s.active_proxy_ports)

    def test_started_epoch_persists_and_recovery_elapsed_does_not_reset(self):
        d=tempfile.TemporaryDirectory(); self._tmp.append(d); path=Path(d.name)/'j.json'; s=w.Supervisor(config(),FakeController(complete_failures=5),FakeAds(),FakeMP(),False,path); x,_=s.register(job(reserved=100)); x.started_epoch=time.time()-47; x.state='running'; s.persist_journal()
        s2=w.Supervisor(config(),FakeController(complete_failures=5),FakeAds(),FakeMP(),False,path); y=s2.reporting[1]; self.assertLessEqual(abs(y.started_epoch-x.started_epoch),.01); self.assertGreaterEqual(s2.progress_payload(y)['elapsed_seconds'],46); y.result={'status':'failed'}; y.cleanup_confirmed=True; self.assertGreaterEqual(s2.completion_payload(y)['actual_seconds'],46)

    def test_cleanup_keeps_ownership_uncertain_locked(self):
        s=self.supervisor(); x,_=s.register(job()); x.ownership_uncertain=True
        self.assertFalse(s.cleanup(x)); self.assertFalse(x.cleanup_confirmed); self.assertEqual(x.cleanup_state,'uncertain'); self.assertIn(x.profile_id,s.active_profiles); self.assertIn(x.proxy_port,s.active_proxy_ports)

    def test_directive_cancel_escalates_at_deadline(self):
        s=self.supervisor(); x,_=s.register(job()); x.process=FakeProcess(True); x.cancel_event=FakeEvent(); s.request_cancel(x,'stop_and_cleanup'); x.cancel_requested_at=time.monotonic()-2; s.tick()
        self.assertGreater(x.process.killed,0); self.assertNotIn(1,s.executions)

    def test_cleanup_timeout_bounds_waits(self):
        launch=time.time()-1; proc=Proc(20,launch,r'C:\adspower_global\FlowerBrowser.exe',['FlowerBrowser.exe','p1']); ps=FakePsutil([proc],alive=True)
        s=self.supervisor(psutil=ps,ads=FakeAds(stop_failures=2)); x,_=s.register(job()); x.owned_launch=True; x.launch_evidence={'profile_id':'p1','launch_started_at':launch}
        s.cleanup(x); self.assertTrue(all(timeout <= s.cfg.cleanup_timeout_seconds for _,timeout in ps.waits))

    def test_shutdown_returns_pending_reporting_ids(self):
        c=FakeController(complete_failures=100); s=self.supervisor(controller=c); x,_=s.register(job()); x.process=FakeProcess(False); x.result={'status':'failed'}; x.cleanup_confirmed=True
        s.finalize(x); self.assertEqual(s.wait_for_shutdown(.01),[1])

    def test_forward_milestones(self):
        class State:
            def __init__(self): self.points=[]
            def checkpoint(self,*a,**k): pass
            def forward(self,name,**kw): self.points.append(name)
            def sleep(self,*a): pass
            def has_seen(self,*a): return False
        class Body: text='ok'
        class Driver:
            current_url='https://x.com/home'; title='home'
            def set_page_load_timeout(self,*a): pass
            def get(self,*a): pass
            def find_element(self,*a): return Body()
            def find_elements(self,*a): return []
            def execute_script(self,script,*args): return [1000,700] if 'innerWidth' in script else None
        state=State(); b=w.BrowserRun(Driver(),state,time.monotonic()+100); b.navigate('https://x.com/home'); b.scroll(10)
        self.assertIn('navigation_returned',state.points); self.assertIn('account_state_dom_batch',state.points); self.assertIn('scroll_returned',state.points)

    def test_frozen_job_does_not_block_peers(self):
        s=self.supervisor(); xs=[]
        for i in range(1,4):
            x,_=s.register(job(i,reserved=100)); x.process=FakeProcess(True); x.cancel_event=FakeEvent(); x.last_progress_sent=time.monotonic(); xs.append(x)
        xs[0].last_forward_progress_at=time.monotonic()-31
        s.tick()
        self.assertGreater(xs[0].process.killed,0)
        self.assertTrue(xs[1].process.is_alive()); self.assertTrue(xs[2].process.is_alive())

    def test_completion_reconciliation(self):
        c=FakeController(complete_failures=1); s=self.supervisor(controller=c); x,_=s.register(job()); x.result={'status':'succeeded'}; x.cleanup_confirmed=True
        s.finalize(x)
        self.assertIn(1,s.reporting); self.assertEqual(s.available_slots(),3)
        x.next_completion_attempt=0; s.tick()
        self.assertNotIn(1,s.reporting)

    def test_shutdown(self):
        s=self.supervisor()
        for i in range(1,3):
            x,_=s.register(job(i)); x.process=FakeProcess(True); x.cancel_event=FakeEvent(); x.owned_launch=False
        remaining=s.wait_for_shutdown(.1)
        self.assertEqual(remaining,[]); self.assertTrue(s.draining); self.assertEqual(s.available_slots(),0)


if __name__ == '__main__': unittest.main()

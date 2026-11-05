"""Offline checks for truthful queue observation and public timeline joins."""
from copy import deepcopy
import json
import unittest

from qoder_byoc_sandbox_example.demos.report_analyst.observation import WorkObserver, build_observation


class WorkAPI:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.stats = dict(type='work_queue_stats',depth=3,pending=2,oldest_queued_at='2026-10-30T01:00:00Z',workers_polling=7)
        self.work = dict(id='work_1',type='work',environment_id='env_1',state='active',
                         data=dict(type='session',id='sess_1'),created_at='2026-10-30T01:00:00Z',
                         latest_heartbeat_at='2026-10-30T01:01:00Z',stopped_at=None,
                         metadata={'private':'do-not-expose'},secret='private-work-secret')

    def request(self, method, path):
        self.calls.append((method,path))
        if self.fail:
            raise RuntimeError('Bearer private-network-secret')
        return deepcopy(self.stats if path.endswith('/stats') else self.work)


class WorkObserverTests(unittest.TestCase):
    def setUp(self):
        self.api = WorkAPI()
        self.now = 100
        self.observer = WorkObserver(self.api,'env_1','sess_1',clock=lambda:self.now)

    def test_only_scoped_reads_and_cache_retain_actual_counter_semantics(self):
        first = self.observer.snapshot('work_1')
        self.assertEqual(first['state'],'ready')
        self.assertEqual(first['stats'],self.api.stats)
        self.assertEqual(first['observed_at'],100)
        self.assertNotIn('metadata',first['work'])
        self.assertNotIn('private-work-secret',json.dumps(first))
        self.assertIn('30',first['workers_polling_meaning'])
        first['stats']['depth']=999
        self.now=102
        second=self.observer.snapshot('work_1')
        self.assertEqual(second['stats']['depth'],3)
        self.assertEqual(second['observed_at'],100)
        self.assertEqual(self.api.calls,[('GET','/environments/env_1/work/stats'),('GET','/environments/env_1/work/work_1')])

    def test_stats_only_does_not_fetch_or_infer_work(self):
        data=self.observer.snapshot()
        self.assertIsNone(data['work'])
        self.assertEqual(self.api.calls,[('GET','/environments/env_1/work/stats')])

    def test_failure_retains_last_success_without_new_timestamp_or_zero_counts(self):
        self.observer.snapshot('work_1')
        self.now=106
        self.api.fail=True
        data=self.observer.snapshot('work_1')
        self.assertEqual(data['state'],'unavailable')
        self.assertTrue(data['stale'])
        self.assertEqual(data['observed_at'],100)
        self.assertEqual(data['stats']['depth'],3)
        self.assertNotIn('private-network-secret',json.dumps(data))
        self.now=107
        self.observer.snapshot('work_1')
        self.assertEqual(len(self.api.calls),3,'Failures also obey the cache window')
        empty=WorkObserver(self.api,'env_2','sess_2').snapshot()
        self.assertIsNone(empty['stats'])
        self.assertIsNone(empty['observed_at'])

    def test_invalid_ids_never_issue_requests(self):
        for env,session,work in [('', 'sess_1',None),('env/other','sess_1',None),('env_1','',None),('env_1','sess_1','../../private')]:
            with self.subTest(env=env,session=session,work=work):
                observer=WorkObserver(self.api,env,session)
                self.assertEqual(observer.snapshot(work)['state'],'unavailable')
        self.assertEqual(self.api.calls,[])

    def test_new_work_lookup_failure_keeps_environment_stats_but_not_another_work(self):
        self.observer.snapshot('work_1')
        self.now=106
        self.api.fail=True
        data=self.observer.snapshot('work_2')
        self.assertEqual(data['stats']['depth'],3)
        self.assertEqual(data['observed_at'],100)
        self.assertIsNone(data['work'])

    def test_count_types_ranges_and_work_ownership_are_validated(self):
        for value in [-1, True, 1.5, '3', 2**53]:
            with self.subTest(value=value):
                self.api.stats['depth']=value
                data=WorkObserver(self.api,'env_1','sess_1').snapshot()
                self.assertIsNone(data['stats'])
        self.api.stats['depth']=3
        self.api.stats['oldest_queued_at']=123
        self.assertIsNone(WorkObserver(self.api,'env_1','sess_1').snapshot()['stats'])
        self.api.stats['oldest_queued_at']='2026-10-30T01:00:00Z'
        for key,value in [('id','work_2'),('environment_id','env_2'),('data',dict(type='session',id='sess_2')),
                          ('state','invented'),('created_at','yesterday')]:
            with self.subTest(key=key):
                original=self.api.work[key]
                self.api.work[key]=value
                self.assertIsNone(WorkObserver(self.api,'env_1','sess_1').snapshot('work_1')['work'])
                self.api.work[key]=original


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.session=dict(trace=[
            dict(id='msg_1',type='user.message',message_id='msg_1',time=10,observed_at=11,text='分析数据'),
            dict(id='model_1',type='session.model_request_start',message_id='msg_1',time=12,observed_at=13),
            dict(id='model_2',type='session.model_request_end',model_request_start_id='model_1',message_id='msg_1',time=14,observed_at=15),
            dict(id='tool_1',type='agent.custom_tool_use',message_id='msg_1',time=16,observed_at=17,tool='run_python'),
            dict(id='result_1',type='user.custom_tool_result',message_id='msg_1',time=20,observed_at=21,tool_use_id='tool_1',is_error=False),
        ],tools=[dict(id='tool_1',tool='run_python',message_id='msg_1',requested_at=16,posted_at=20,state='posted')])
        self.execution=dict(events=[dict(id='tool_1',tool='run_python',title='运行 Python 分析',message_id='msg_1',role='analysis',
                                         observed_at=17,started_at=18,finished_at=19,elapsed_ms=1200,state='completed',
                                         input='{"script":"analysis.py"}',code='print(42)',output='42')],
                            runtime=[dict(kind='work_poll',time=9,work_id='work_1',result='returned',state='queued'),
                                     dict(kind='work_ack',time=9.1,work_id='work_1',state='starting'),
                                     dict(kind='heartbeat_confirmed',time=19,work_id='work_1',ttl_seconds=60)],observed_at=21)

    def build(self, now=22):
        return build_observation({},self.session,self.execution,{},clock=lambda:now)

    def test_separate_request_execution_and_result_join_one_tool_without_duplicate_fallback(self):
        before=deepcopy((self.session,self.execution))
        data=self.build()
        tool_events=[event for event in data['events'] if event['tool_id']=='tool_1']
        self.assertEqual([event['kind'] for event in tool_events],
                         ['agent.custom_tool_use','tool_received','tool_started','tool_execution','user.custom_tool_result'])
        phases = {event['kind']: event for event in tool_events}
        self.assertEqual(phases['agent.custom_tool_use']['input'],'{"script":"analysis.py"}')
        self.assertEqual(phases['tool_started']['code'],'print(42)')
        self.assertEqual(phases['tool_execution']['time'],19)
        self.assertEqual(phases['tool_started']['time'],18)
        self.assertEqual(phases['user.custom_tool_result']['output'],'42')
        self.assertEqual(phases['tool_execution']['elapsed_ms'],1200)
        self.assertNotIn('elapsed_ms',phases['tool_started'])
        self.assertNotIn('elapsed_ms',phases['agent.custom_tool_use'])
        self.assertEqual(before,(self.session,self.execution),'Inputs must not be mutated')

    def test_model_duration_requires_real_matching_start_and_end_ids(self):
        self.session['trace'][1]['type']='span.model_request_start'
        self.session['trace'][2]['type']='span.model_request_end'
        models=[event for event in self.build()['events'] if event['category']=='model']
        self.assertEqual(models[-1]['elapsed_ms'],2000)
        self.session['trace'][2]['model_request_start_id']='other_model'
        models=[event for event in self.build()['events'] if event['category']=='model']
        self.assertNotIn('elapsed_ms',models[-1])

    def test_orphan_execution_has_no_invented_start_or_elapsed(self):
        call=self.execution['events'][0]
        del call['started_at']
        call['history_incomplete']=True
        self.execution['history_incomplete']=True
        event=next(event for event in self.build()['events'] if event['kind']=='tool_execution')
        self.assertEqual(event['time'],19)
        self.assertNotIn('elapsed_ms',event)
        self.assertTrue(self.build()['history_incomplete'])

    def test_orphan_execution_uses_matching_public_tool_message_without_inventing_work_or_timing(self):
        call=self.execution['events'][0]
        del call['message_id']
        del call['started_at']
        call['history_incomplete']=True
        event=next(event for event in self.build()['events'] if event['kind']=='tool_execution')
        self.assertEqual(event['message_id'],'msg_1')
        self.assertEqual(event['time'],19)
        self.assertIsNone(event['work_id'])
        self.assertNotIn('elapsed_ms',event)
        self.assertNotIn('message_id',call,'Normalization must not mutate the retained log record')
        call['message_id']='recorded-parent'
        event=next(event for event in self.build()['events'] if event['kind']=='tool_execution')
        self.assertEqual(event['message_id'],'recorded-parent')
        del call['message_id']
        self.session['tools'][0]['id']='unrelated-tool'
        event=next(event for event in self.build()['events'] if event['kind']=='tool_execution')
        self.assertIsNone(event['message_id'])

    def test_public_running_and_rescheduling_status_are_preserved(self):
        self.session['trace'].extend([
            dict(id='running',type='session.status_running',message_id='msg_1',time=21),
            dict(id='rescheduling',type='session.status_rescheduling',message_id='msg_1',time=22),
        ])
        events=[event for event in self.build()['events'] if event['category']=='session']
        self.assertEqual([event['kind'] for event in events],['session.status_running','session.status_rescheduling'])

    def test_worker_health_uses_heartbeat_ttl_stop_and_observation_freshness(self):
        self.assertEqual(self.build()['worker']['state'],'healthy')
        self.execution['observed_at']=80
        self.assertEqual(self.build(now=80)['worker']['state'],'expired')
        self.execution['observed_at']=21
        self.execution['stale']=True
        self.assertEqual(self.build()['worker']['state'],'unknown')
        self.execution['stale']=False
        self.execution['runtime'].append(dict(kind='worker_stopped',time=20,work_id='work_1'))
        self.assertEqual(self.build()['worker']['state'],'stopped')
        self.execution['runtime']=[]
        observed=build_observation({},self.session,self.execution,{'stats':{'workers_polling':99}},clock=lambda:22)
        self.assertEqual(observed['worker']['state'],'unknown')

    def test_stable_deduplicated_ids_and_explicit_bounded_history(self):
        self.session['trace'] += deepcopy(self.session['trace'])
        self.execution['runtime'] += deepcopy(self.execution['runtime'])
        first=self.build()
        second=self.build()
        self.assertEqual([event['id'] for event in first['events']],[event['id'] for event in second['events']])
        self.assertEqual(len(first['events']),len({event['id'] for event in first['events']}))
        self.session['trace']=[dict(id='msg_'+str(n),type='user.message',time=n,observed_at=n,text='message') for n in range(700)]
        self.assertEqual(len(self.build()['events']),600)
        self.assertTrue(self.build()['history_incomplete'])

    def test_historical_events_keep_original_work_and_qca_events_do_not_borrow_live_work(self):
        self.execution['events'][0]['work_id']='work_old'
        self.execution['runtime'].append(dict(kind='heartbeat_confirmed',time=21,work_id='work_new',ttl_seconds=60))
        data=build_observation({},self.session,self.execution,{'work':{'id':'work_current'}},clock=lambda:22)
        heartbeat_events=[event for event in data['events'] if event['category']=='heartbeat']
        self.assertEqual([event['work_id'] for event in heartbeat_events],['work_1','work_new'])
        local=next(event for event in data['events'] if event['kind']=='tool_execution')
        self.assertEqual(local['work_id'],'work_old')
        qca=[event for event in data['events'] if event['id'].startswith('qca:')]
        self.assertTrue(all(event['work_id'] is None for event in qca))

    def test_history_normalization_can_exceed_live_window_with_explicit_bound(self):
        self.session['trace']=[dict(id='msg_'+str(n),type='user.message',message_id='msg_root',time=n,observed_at=n,text='公开消息') for n in range(1300)]
        self.session['tools']=[]
        self.execution={'events':[],'runtime':[]}
        replay=build_observation({},self.session,self.execution,{},clock=lambda:1400,max_events=5000)
        self.assertEqual(len(replay['events']),1300)
        self.assertEqual(replay['events'][0]['id'],'qca:msg_0')
        self.assertFalse(replay['history_incomplete'])
        bounded=build_observation({},self.session,self.execution,{},clock=lambda:1400,max_events=1000)
        self.assertEqual(len(bounded['events']),1000)
        self.assertTrue(bounded['history_incomplete'])

    def test_history_keeps_execution_truncation_honest(self):
        self.execution['events']=[]
        self.execution['truncated']=True
        replay=build_observation({},self.session,self.execution,{},clock=lambda:22,max_events=5000)
        self.assertTrue(replay['history_incomplete'])
        self.assertFalse(any(event['kind'] in {'tool_started','tool_execution'} for event in replay['events']))


if __name__=='__main__':
    unittest.main()

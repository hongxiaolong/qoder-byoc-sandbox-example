"""Trace correlation, truthful timing, and run-bound API integration."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qoder_byoc_sandbox_example.demos.report_analyst.execution import execution_snapshot
from qoder_byoc_sandbox_example.demos.report_analyst.lifecycle import record_created, public_history
from qoder_byoc_sandbox_example.demos.report_analyst.observation import build_observation
from qoder_byoc_sandbox_example.demos.report_analyst.shared import bind_trace
from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder


def rows(data):
    return [row for group in data['tracing']['groups'] for row in group['rows']]


class TracingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = dict(run_id='run-a', session_id='sess-a')
        self.session = dict(active_message_id=None, turns=[dict(id='msg-a', state='completed')], tools=[
            dict(id='tool-a', tool='run_python', message_id='msg-a', state='completed')], trace=[
            dict(id='msg-a', type='user.message', message_id='msg-a', time=10, text='分析数据'),
            dict(id='model-a', type='span.model_request_start', message_id='msg-a', time=11),
            dict(id='model-b', type='span.model_request_end', message_id='msg-a', time=11.4, model_request_start_id='model-a'),
            dict(id='tool-a', type='agent.tool_use', message_id='msg-a', time=12, tool='run_python'),
            dict(id='result-a', type='user.tool_result', message_id='msg-a', time=14, tool_use_id='tool-a'),
            dict(id='reply-a', type='agent.message', message_id='msg-a', time=15, text='已完成'),
            dict(id='idle-a', type='session.status_idle', message_id='msg-a', time=16),
        ])
        self.execution = dict(events=[dict(id='tool-a', tool='run_python', title='运行 Python', message_id='msg-a',
            role='analysis', state='completed', started_at=12.2, finished_at=13.2, elapsed_ms=27,
            input='input.csv', code='print(1)', output='exit_code=0')], runtime=[
            dict(kind='tool_stage', time=12.4, tool_use_id='tool-a', stage='execute', role='analysis'),
            dict(kind='heartbeat_failed', time=18, work_id='work-a', error='租约失效')])

    def build(self, api_trace=None):
        return build_observation(self.status, self.session, self.execution, {}, clock=lambda:20, api_trace=api_trace)

    def capture(self, *, method='POST', path='/sessions/sess-a/events', request=None, response=None, run_id='run-a', identifier='http-a'):
        recorder = APITraceRecorder(self.root/(identifier + '.jsonl'), run_id, session_id='sess-a', secrets=('test-secret-token',))
        recorder.record(dict(id=identifier, method=method, path=path, state='success', status_code=201, attempt=1,
            started_at=13.4, finished_at=13.5, elapsed_ms=19,
            request=dict(headers={'Content-Type':'application/json'}, body=request),
            response=dict(headers={'X-Request-ID':'req-a'}, body=response)))
        return recorder.snapshot()

    def test_tool_phases_merge_without_attaching_to_preceding_model(self):
        data = self.build()
        tool = next(r for r in rows(data) if r['operation']=='tool')
        self.assertEqual(tool['id'], 'tool:tool-a')
        self.assertEqual(tool['event_ids'], ['qca:tool-a', 'execution-start:tool-a', 'execution:tool-a', 'qca:result-a'])
        self.assertNotIn('parent_id', tool)
        self.assertEqual(tool['elapsed_ms'], 27)  # Actual monotonic sample, never wall-clock difference.
        self.assertEqual(tool['measurement'], 'monotonic')
        self.assertEqual(tool['timing_state'], 'measured')
        stage = next(r for r in rows(data) if r['operation']=='stage')
        self.assertEqual(stage['parent_id'], tool['id'])
        self.assertEqual(stage['timing_state'], 'instant')

    def test_model_pairs_exact_start_id_and_messages_remain_points(self):
        data = self.build()
        model = next(r for r in rows(data) if r['operation']=='model')
        self.assertEqual(model['event_ids'], ['qca:model-a', 'qca:model-b'])
        self.assertAlmostEqual(model['elapsed_ms'], 400)
        self.assertEqual(model['measurement'], 'qca_paired_timestamps')
        for item in rows(data):
            if item['operation']=='message':
                self.assertEqual(item['type'], 'event')
                self.assertEqual(item['timing_state'], 'instant')
                self.assertNotIn('elapsed_ms', item)

    def test_missing_model_parent_is_retained_without_fabricating_duration(self):
        self.session['trace'][2]['model_request_start_id'] = 'missing-model'
        models = [r for r in rows(self.build()) if r['operation']=='model']
        self.assertEqual(len(models), 2)
        self.assertTrue(all(r['timing_state']=='incomplete' and 'elapsed_ms' not in r for r in models))

    def test_unfinished_model_is_running_only_for_recorded_active_turn(self):
        del self.session['trace'][2]
        self.session['active_message_id']='msg-a'
        self.session['turns'][0]['state']='running'
        model = next(r for r in rows(self.build()) if r['operation']=='model')
        self.assertEqual(model['timing_state'],'running')
        self.assertNotIn('elapsed_ms',model)

    def test_orphan_tool_and_stage_stay_accessible_in_background(self):
        self.session = dict(trace=[],tools=[],turns=[])
        self.execution['events'][0].update(message_id=None,started_at=None,finished_at=None,history_incomplete=True)
        self.execution['runtime'][0]['tool_use_id']='missing-tool'
        data = self.build()
        group = next(g for g in data['tracing']['groups'] if g['id']=='unassigned')
        tool = next(r for r in group['rows'] if r['operation']=='tool')
        stage = next(r for r in group['rows'] if r['operation']=='stage')
        self.assertEqual(tool['timing_state'],'incomplete')
        self.assertNotIn('elapsed_ms',tool)
        self.assertNotIn('parent_id',stage)
        self.assertTrue(any(r['state']=='heartbeat_failed' for r in rows(data)))

    def test_session_and_work_are_background_even_when_the_session_event_has_message_id(self):
        data = self.build()
        background = next(g for g in data['tracing']['groups'] if g['scope']=='background')
        self.assertTrue(any(r['operation']=='session' for r in background['rows']))
        self.assertTrue(any(r['operation']=='work' for r in background['rows']))
        self.assertTrue(all('message_id' not in r for r in background['rows'] if r['operation']=='work'))
        self.assertEqual(next(r for r in background['rows'] if r['operation']=='session')['message_id'],'msg-a')

    def test_actual_api_records_append_only_http_events_and_keep_safe_original(self):
        before = self.build()
        capture = self.capture(request={'events':[{'type':'user.tool_result','tool_use_id':'tool-a','content':[{'type':'text','text':'done'}]}]},
                               response={'data':[{'id':'posted-a','type':'user.tool_result','tool_use_id':'tool-a'}]})
        data = self.build(capture)
        self.assertEqual([e for e in data['events'] if e['category']!='http'], before['events'])
        api = next(r for r in rows(data) if r['operation']=='http')
        self.assertEqual(api['parent_id'],'tool:tool-a')
        self.assertEqual(api['message_id'],'msg-a')
        self.assertEqual(api['elapsed_ms'],19)
        self.assertEqual(api['event_ids'],['api:http-a'])
        record = data['api_trace']['requests'][0]
        self.assertEqual(record['response']['headers']['x-request-id'],'req-a')
        self.assertEqual(record['request']['body']['events'][0]['tool_use_id'],'tool-a')

    def test_get_api_cannot_take_parent_from_returned_tool_results_or_session_id(self):
        capture = self.capture(method='GET', response={'data':[{'id':'result-a','type':'user.tool_result','tool_use_id':'tool-a'}]})
        data = self.build(capture)
        background = next(g for g in data['tracing']['groups'] if g['scope']=='background')
        api = next(r for r in background['rows'] if r['operation']=='http')
        self.assertNotIn('parent_id',api)
        self.assertNotIn('message_id',api)

    def test_api_cannot_reuse_wrong_run_and_missing_capture_is_not_http(self):
        capture = self.capture(run_id='other-run')
        data = self.build(capture)
        self.assertEqual(data['api_trace']['requests'],[])
        self.assertTrue(data['tracing']['history_incomplete'])
        self.assertFalse(any(e['category']=='http' for e in data['events']))
        self.assertFalse(any(r['operation']=='http' for r in rows(self.build())))

    def test_created_resource_is_point_linked_only_to_matching_successful_post(self):
        record_created(self.status,'agent','agent-a')
        capture = self.capture(path='/agents',request={'name':'Agent','metadata':{'demo_run':'run-a'}},response={'id':'agent-a','type':'agent'})
        data = self.build(capture)
        setup = next(g for g in data['tracing']['groups'] if g['scope']=='setup')
        creation = next(r for r in setup['rows'] if r['operation']=='resource')
        self.assertEqual(creation['type'],'event')
        self.assertEqual(creation['parent_id'],'http:http-a')
        self.assertNotIn('elapsed_ms',creation)
        self.assertEqual(len([r for r in setup['rows'] if r['operation']=='http']),1)

    def test_sdk_create_is_measured_span_but_never_http(self):
        record_created(self.status,'analysis','sbx-a',sdk_create=dict(started_at=1,finished_at=4,elapsed_ms=127))
        data=self.build()
        sdk=next(r for r in rows(data) if r['source']=='sandbox_sdk')
        self.assertEqual(sdk['elapsed_ms'],127)
        self.assertEqual(sdk['measurement'],'monotonic')
        created=next(r for r in rows(data) if r.get('parent_id')==sdk['id'])
        self.assertEqual(created['type'],'event')
        self.assertNotIn('elapsed_ms',created)
        self.assertFalse(data['api_trace']['requests'])
        self.assertNotIn('sdk_create',public_history([dict(self.status['lifecycle'][0],sdk_create={'elapsed_ms':'fake'})])[0])

    def test_old_resource_history_has_uncollected_mapping_and_no_invented_request(self):
        record_created(self.status,'session','sess-a')
        data=self.build()
        resource=next(r for r in rows(data) if r['operation']=='resource')
        self.assertIn('POST /sessions',resource['label'])
        self.assertEqual(resource['measurement'],'uncollected')
        self.assertNotIn('elapsed_ms',resource)
        self.assertEqual(data['api_trace']['requests'],[])

    def test_worker_journal_is_bound_to_run_not_current_work_and_cannot_cross_run(self):
        recorder=APITraceRecorder(self.root/'api-trace.jsonl','run-a',session_id='sess-a')
        for identifier in ('work-old','work-new'):
            recorder.record(dict(id='http-'+identifier,method='POST',path='/environments/env-a/work/'+identifier+'/heartbeat',
                state='success',status_code=200,attempt=1,started_at=1,finished_at=2,elapsed_ms=3,
                request={'body':{}},response={'body':{'id':identifier,'data':{'id':'sess-a','type':'session'}}}))
        identity=dict(run_id='run-a',qoder_session_url='https://qoder.com/cloud/sessions/sess-a')
        data=execution_snapshot(self.root,self.root/'worker.jsonl',identity=identity)
        self.assertEqual(len(data['api_trace']['requests']),2)
        identity['run_id']='run-b'
        wrong=execution_snapshot(self.root,self.root/'worker.jsonl',identity=identity)
        self.assertEqual(wrong['api_trace']['requests'],[])
        self.assertTrue(wrong['api_trace']['history_incomplete'])

    def test_control_mutation_capture_excludes_dashboard_reads_and_redacts_credentials(self):
        class Client: pass
        client=Client()
        with patch('qoder_byoc_sandbox_example.demos.report_analyst.shared.results',return_value=self.root):
            bind_trace(client,self.status,{'TOKEN':'test-secret-token'},mutations_only=True)
        source=dict(id='get-a',method='GET',path='/sessions/sess-a',state='success',status_code=200,attempt=1,started_at=1,finished_at=2,elapsed_ms=1,
                    request={'headers':{'authorization':'Bearer test-secret-token'}},response={'body':{'id':'sess-a','name':'test-secret-token'}})
        client.observer(source)
        self.assertFalse((self.root/'api-trace.jsonl').exists())
        client.observer(dict(source,id='post-a',method='POST'))
        data=APITraceRecorder(self.root/'api-trace.jsonl','run-a').snapshot()
        self.assertEqual(len(data['requests']),1)
        self.assertNotIn('test-secret-token',json.dumps(data))
        self.assertNotIn('authorization',json.dumps(data))

    def test_projection_is_read_only_and_every_trace_reference_resolves(self):
        status,session,execution=map(copy.deepcopy,(self.status,self.session,self.execution))
        data=self.build()
        self.assertEqual((self.status,self.session,self.execution),(status,session,execution))
        event_ids={e['id'] for e in data['events']}
        self.assertTrue(all(set(r['event_ids'])<=event_ids for r in rows(data)))
        allowed={'id','type','operation','label','role','state','parent_id','event_ids','tool_id','message_id',
                 'started_at','finished_at','elapsed_ms','timing_state','measurement','source'}
        self.assertTrue(all(set(r)<=allowed for r in rows(data)))

    def test_worker_main_configures_run_journal_before_execution(self):
        from qoder_byoc_sandbox_example.environments.self_hosted import worker as module
        from unittest.mock import Mock
        config=dict(run_id='run-a',base_url='https://qoder.invalid',environment_id='env-a',session_id='sess-a',
                    worker_id='worker-a',workdir=str(self.root),state_dir=str(self.root),max_seconds=1)
        path=self.root/'worker-config.json'
        path.write_text(json.dumps(config))
        def make_worker(client,*_):
            worker=Mock()
            def run(_):
                self.assertIsNotNone(client.observer)
                client.observer(dict(id='http-worker-main',method='POST',path='/environments/env-a/work/work-a/heartbeat',
                    state='success',status_code=200,attempt=1,started_at=1,finished_at=2,elapsed_ms=4,
                    request={'headers':{'Authorization':'Bearer worker-private-token'},'body':{}},
                    response={'body':{'id':'work-a','data':{'id':'sess-a','type':'session'}}}))
            worker.run.side_effect=run
            return worker
        with patch.dict('os.environ',{'QODER_ACCESS_TOKEN':'worker-private-token'}), \
             patch.object(module,'Worker',side_effect=make_worker), patch.object(module.signal,'signal'):
            module.main(['--config',str(path)])
        captured=execution_snapshot(self.root,self.root/'worker.jsonl',identity=dict(run_id='run-a',qoder_session_url='https://qoder.com/cloud/sessions/sess-a'))
        self.assertEqual(len(captured['api_trace']['requests']),1)
        self.assertEqual(captured['api_trace']['requests'][0]['id'],'http-worker-main')
        self.assertNotIn('worker-private-token',json.dumps(captured))

    def test_http_raw_point_is_completion_time_but_span_retains_actual_start(self):
        capture=self.capture()
        data=self.build(capture)
        point=next(e for e in data['events'] if e['id']=='api:http-a')
        span=next(r for r in rows(data) if r['id']=='http:http-a')
        self.assertEqual(point['time'],13.5)
        self.assertEqual(span['started_at'],13.4)
        self.assertEqual(span['finished_at'],13.5)
        self.assertNotIn(point,[e for e in data['events'] if e['time'] is not None and e['time']<=13.45])
        ids=[e['id'] for e in data['events']]
        self.assertLess(ids.index('execution:tool-a'),ids.index('api:http-a'))
        self.assertLess(ids.index('api:http-a'),ids.index('qca:result-a'))

    def test_enriched_raw_sort_uses_observed_fallback_and_keeps_stable_ties(self):
        from qoder_byoc_sandbox_example.demos.report_analyst.tracing import enrich_observation
        original=[dict(id='late',kind='session.status_idle',category='session',time=15,observed_at=15),
                  dict(id='observed-tie',kind='session.status_idle',category='session',time=None,observed_at=13.5),
                  dict(id='source-tie',kind='session.status_idle',category='session',time=13.5,observed_at=16),
                  dict(id='unknown',kind='session.status_idle',category='session',time=None,observed_at=None)]
        data=enrich_observation(dict(status=self.status,session={},execution={},events=original),self.capture())
        self.assertEqual([e['id'] for e in data['events']],['observed-tie','source-tie','api:http-a','late','unknown'])
        self.assertEqual(data['events'][0]['time'],None)
        self.assertEqual(original[0]['id'],'late')

    def test_session_idle_preserves_recorded_message_without_borrowing_current_turn(self):
        self.session['active_message_id']='msg-b'
        self.session['turns'].append(dict(id='msg-b',state='running'))
        self.session['trace'].extend([
            dict(id='msg-b',type='user.message',message_id='msg-b',time=17,text='继续'),
            dict(id='late-idle-a',type='session.status_idle',message_id='msg-a',time=18),
            dict(id='unassigned-idle',type='session.status_idle',time=19)])
        data=self.build()
        background=next(g for g in data['tracing']['groups'] if g['id']=='background')
        explicit=next(r for r in background['rows'] if r['id']=='event:qca:late-idle-a')
        missing=next(r for r in background['rows'] if r['id']=='event:qca:unassigned-idle')
        self.assertEqual(explicit['message_id'],'msg-a')
        self.assertNotIn('message_id',missing)
        current=next(g for g in data['tracing']['groups'] if g['id']=='turn:msg-b')
        self.assertFalse(any(r['operation']=='session' for r in current['rows']))

    def test_turn_api_selection_excludes_future_input_background_and_mixed_batches(self):
        from qoder_byoc_sandbox_example.demos.report_analyst.tracing import select_turn_api_trace
        records=[]
        def add(identifier,request,response,**kwargs):
            records.extend(self.capture(identifier=identifier,request=request,response=response,**kwargs)['requests'])
        add('input-a',{'events':[{'type':'user.message','content':[{'type':'text','text':'first-input'}]}]},
            {'data':[{'id':'msg-a','type':'user.message'}]})
        add('tool-a',{'events':[{'type':'user.tool_result','tool_use_id':'tool-a'}]},
            {'data':[{'id':'result-a','type':'user.tool_result','tool_use_id':'tool-a'}]})
        add('input-b',{'events':[{'type':'user.message','content':[{'type':'text','text':'future-input-marker'}]}]},
            {'data':[{'id':'msg-b','type':'user.message'}]})
        add('poll',None,{'data':[{'id':'msg-b','type':'user.message','content':[{'type':'text','text':'future-poll-marker'}]}]},method='GET')
        add('setup',{'name':'future-resource-marker'},{'id':'agent-a'},path='/agents')
        add('mixed',{'events':[{'type':'user.tool_result','tool_use_id':'tool-a'},
                               {'type':'user.tool_result','tool_use_id':'tool-b','content':[{'type':'text','text':'future-mixed-marker'}]}]}, {'data':[]})
        add('response-only',{}, {'data':[{'id':'result-a','type':'user.tool_result','tool_use_id':'tool-a'}]})
        capture=dict(requests=records,sources={r['id']:'control' for r in records},history_incomplete=False)
        selected=select_turn_api_trace(capture,'msg-a',{'tool-a'})
        self.assertEqual({r['id'] for r in selected['requests']},{'input-a','tool-a'})
        self.assertEqual(set(selected['sources']),{'input-a','tool-a'})
        self.assertNotIn('future-',json.dumps(selected))
        self.assertEqual(len(capture['requests']),7)

    def test_explicit_post_message_association_remains_scoped_without_session_inference(self):
        from qoder_byoc_sandbox_example.demos.report_analyst.tracing import select_turn_api_trace
        capture=self.capture(request={'events':[{'type':'user.interrupt','message_id':'msg-a'}]},response={'data':[]})
        selected=select_turn_api_trace(capture,'msg-a',set())
        self.assertEqual(len(selected['requests']),1)
        self.assertEqual(select_turn_api_trace(capture,'msg-b',set())['requests'],[])
        http=next(r for r in rows(self.build(selected)) if r['operation']=='http')
        self.assertEqual(http['message_id'],'msg-a')

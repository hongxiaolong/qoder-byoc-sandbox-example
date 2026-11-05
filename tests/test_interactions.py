"""Component handoffs, independent of internal status/HTTP record volume."""
import copy
from pathlib import Path
import tempfile
import unittest

from test_live_session import EventAPI
from qoder_byoc_sandbox_example.demos.report_analyst.live_session import LiveSession
from qoder_byoc_sandbox_example.demos.report_analyst.observation import build_observation


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = EventAPI()
        self.live = LiveSession(self.api, 'sess-a', Path(self.folder.name) / 'events.sqlite3')
        self.message = self.api.event('user.message', content=[dict(type='text', text='分析数据')])
        self.call = self.api.event('agent.custom_tool_use', name='run_python')
        self.execution = dict(events=[dict(id=self.call['id'], tool='run_python',
            message_id=self.message['id'], state='running', started_at=102, role='analysis')], runtime=[
                dict(kind='tool_stage', time=102.1, tool_use_id=self.call['id'], stage='python_dispatch', role='analysis')])

    def observe(self, session=None, live=None, limit=600):
        live = live or self.live
        data = build_observation(dict(run_id='run-a', session_id='sess-a'),
            session if session is not None else live.snapshot(), self.execution, {}, max_events=limit)
        data['interactions'] = live.project_interactions(data)
        return data

    def conversation(self, data):
        return next(c for c in data['interactions']['conversations'] if c['message_id'] == self.message['id'])

    def numbers(self, data):
        return {s['id']: s['number'] for s in self.conversation(data)['steps']}

    def finish(self):
        self.execution['events'][0].update(state='completed', finished_at=104, elapsed_ms=2000)
        if self.execution['events'][0]['tool'] == 'run_python':
            self.execution['runtime'].append(dict(kind='tool_stage', time=104, tool_use_id=self.call['id'], stage='python_return', role='analysis'))
        self.api.event('user.custom_tool_result', custom_tool_use_id=self.call['id'])
        self.api.event('agent.message', content=[dict(type='text', text='完成')])
        self.api.event('session.status_idle', stop_reason={'type':'end_turn'})

    def test_python_crosses_two_leaf_components_and_internal_phases_never_count(self):
        self.finish()
        for i in range(20):
            self.execution['runtime'].append(dict(kind='tool_stage', time=103 + i / 100,
                tool_use_id=self.call['id'], stage='python_progress', role='analysis'))
        data = self.observe()
        steps = self.conversation(data)['steps']
        self.assertEqual(data['interactions']['version'], 4)
        self.assertEqual([(s['source'], s['target']) for s in steps], [
            ('input', 'session'), ('loop', 'events'), ('events', 'worker'),
            ('worker', 'python'), ('python', 'worker'), ('worker', 'events'), ('loop', 'session')])
        self.assertEqual([s['number'] for s in steps], list(range(1, 8)))
        self.assertTrue(all(s['source'] != s['target'] for s in steps))
        self.assertEqual(self.conversation(data)['total_steps'], 7)

    def test_read_file_executes_inside_worker_without_a_python_handoff(self):
        self.api.feed[1]['name'] = 'Read'
        self.execution['events'][0]['tool'] = 'Read'
        self.execution['runtime'] = []
        self.finish()
        steps = self.conversation(self.observe())['steps']
        self.assertEqual([(s['source'], s['target']) for s in steps], [
            ('input', 'session'), ('loop', 'events'), ('events', 'worker'),
            ('worker', 'events'), ('loop', 'session')])

    def test_models_internal_status_heartbeat_and_http_do_not_inflate_numbers(self):
        first = self.observe()
        for index in range(120):
            start = self.api.event('span.model_request_start')
            self.api.event('span.model_request_end', model_request_start_id=start['id'])
            self.execution['runtime'].extend([
                dict(kind='heartbeat_confirmed', time=1000 + index, work_id='work-a'),
                dict(kind='work_poll', time=1000 + index, work_id='work-a')])
        data = self.observe()
        data['events'].append(dict(id='http', kind='http.request', message_id=self.message['id'], tool_id=self.call['id']))
        data['interactions'] = self.live.project_interactions(data)
        self.assertEqual(self.conversation(data)['total_steps'], 4)
        self.assertEqual(self.numbers(first), self.numbers(data))
        self.assertFalse(any(i == 'http' for s in self.conversation(data)['steps'] for i in s['event_ids']))

    def test_delayed_running_status_is_not_relabelled_as_initial_dispatch(self):
        for _ in range(30):
            self.api.event('session.status_running', message_id=self.message['id'])
        steps = self.conversation(self.observe())['steps']
        self.assertEqual(sum((s['source'], s['target']) == ('session', 'loop') for s in steps), 0)

    def test_browser_commands_and_csv_are_handoffs_unknown_progress_is_not(self):
        self.api.feed[1]['name'] = 'download_report'
        self.execution['events'][0]['tool'] = 'download_report'
        self.execution['runtime'] = []
        for index, stage in enumerate(['browser_connect', 'browser_filter', 'browser_download', 'csv_transfer', 'download_progress']):
            self.execution['runtime'].append(dict(kind='tool_stage', time=103 + index / 10,
                tool_use_id=self.call['id'], stage=stage, role='browser' if stage != 'csv_transfer' else 'transfer'))
        self.finish()
        data = self.observe()
        pairs = [(s['source'], s['target']) for s in self.conversation(data)['steps']]
        self.assertEqual(pairs.count(('worker', 'browser')), 1)
        self.assertEqual(pairs.count(('browser', 'worker')), 1)
        self.assertNotIn(('worker', 'python'), pairs)
        self.assertEqual(self.conversation(data)['total_steps'], 7)

    def test_live_replay_restart_and_late_records_share_the_ledger(self):
        first = self.observe()
        self.finish()
        for _ in range(650):
            self.api.event('span.model_request_start')
        live = self.observe()
        replay = self.observe(self.live.snapshot(message_id=self.message['id']), limit=5000)
        self.assertEqual(self.conversation(live)['total_steps'], 7)
        for key, number in self.numbers(first).items():
            self.assertEqual(self.numbers(replay)[key], number)
        restarted = LiveSession(self.api, 'sess-a', self.live.journal)
        self.assertEqual(self.numbers(self.observe(live=restarted)), self.numbers(live))
        # Older real browser command arrives late, before the display window.
        self.execution['runtime'].append(dict(kind='tool_stage', time=101, tool_use_id=self.call['id'],
            stage='browser_filter', role='browser'))
        late = self.observe(live=restarted)
        late_replay = self.observe(restarted.snapshot(message_id=self.message['id']), live=restarted, limit=5000)
        self.assertEqual(self.conversation(late)['total_steps'], 8)
        self.assertEqual(max(self.numbers(late_replay).values()), 8)
        for key, number in self.numbers(live).items():
            self.assertEqual(self.numbers(late_replay)[key], number)

    def test_worker_receipt_and_start_update_the_same_delivery(self):
        first = self.observe()
        self.execution['events'][0]['observed_at'] = 101
        second = self.observe()
        self.assertEqual(self.conversation(first)['total_steps'], self.conversation(second)['total_steps'])
        self.assertEqual(self.numbers(first), self.numbers(second))
        received = next(s for s in self.conversation(second)['steps'] if s['target'] == 'worker')
        self.assertIn('execution-received:' + self.call['id'], received['event_ids'])

    def test_result_fallback_and_official_receipt_do_not_duplicate_handoffs(self):
        self.execution['events'][0].update(state='completed', finished_at=104)
        source = self.live.snapshot()
        source['tools'][0]['posted_at'] = 105
        first = self.observe(source)
        result = self.api.event('user.custom_tool_result', custom_tool_use_id=self.call['id'])
        second = self.observe()
        receipt = next(s for s in self.conversation(second)['steps'] if s['flow'] == 'result')
        self.assertIn('qca:' + result['id'], receipt['event_ids'])
        self.assertEqual(self.numbers(first), self.numbers(second))

    def test_old_event_ledger_does_not_seed_component_ordinals(self):
        with self.live.db() as db:
            db.execute('CREATE TABLE flow_steps (message_id, step_key, number, source_kind, tool_id)')
            db.execute('INSERT INTO flow_steps VALUES (?,?,?,?,?)', (self.message['id'], 'old-model', 49, 'model_request_start', None))
        data = self.observe()
        self.assertEqual([s['number'] for s in self.conversation(data)['steps']], [1, 2, 3, 4])

    def test_conversation_number_is_session_order_not_filtered_position(self):
        second = self.api.event('user.message', content=[dict(type='text', text='下一条')])
        self.api.event('agent.message', content=[dict(type='text', text='答复')])
        data = self.observe(self.live.snapshot(message_id=second['id']))
        entry = data['interactions']['conversations'][0]
        self.assertEqual(entry['number'], 2)
        self.assertEqual([s['number'] for s in entry['steps']], [1, 2])

    def test_metadata_only_ledger(self):
        self.observe()
        with self.live.db() as db:
            rows = list(db.execute('SELECT * FROM component_v4_steps'))
        self.assertNotIn('分析数据', repr(rows))
        self.assertNotIn('print(', repr(rows))

    def test_validation_failure_does_not_invent_python_dispatch_or_return(self):
        self.execution['runtime'] = []
        self.execution['events'][0].update(state='error', finished_at=104, output='Python source exceeds demo limit')
        steps = self.conversation(self.observe())['steps']
        self.assertFalse(any('python' in (s['source'], s['target']) for s in steps))
        self.assertEqual(len(steps), 3)

    def test_old_exit_code_evidence_and_new_stages_alias_the_same_boundaries(self):
        self.execution['runtime'] = []
        self.execution['events'][0].update(state='completed', finished_at=104, output='exit_code=0\n42')
        first = self.observe()
        self.assertEqual(self.conversation(first)['total_steps'], 5)
        self.execution['runtime'] = [dict(kind='tool_stage', time=102.1, tool_use_id=self.call['id'], stage='python_dispatch'),
                                     dict(kind='tool_stage', time=104, tool_use_id=self.call['id'], stage='python_return')]
        second = self.observe()
        self.assertEqual(self.numbers(first), self.numbers(second))
        self.assertEqual(len([s for s in self.conversation(second)['steps'] if 'python' in (s['source'], s['target'])]), 2)

    def test_real_qca_initial_model_and_parallel_tool_batch_have_one_publish_handoff(self):
        api = EventAPI()
        self.execution = dict(events=[], runtime=[])
        # Actual QCA order: Session starts before the user event, then model,
        # visible remark and two calls in one requires_action batch.
        api.event('session.status_running')
        message = api.event('user.message', content=[dict(type='text',text='分析数据')])
        model = api.event('span.model_request_start')
        api.event('agent.message',content=[dict(type='text',text='先读取技能和环境')])
        first = api.event('agent.tool_use',name='Read')
        second = api.event('agent.custom_tool_use',name='runtime_evidence')
        api.event('span.model_request_end',model_request_start_id=model['id'])
        api.event('session.status_idle',stop_reason=dict(type='requires_action',event_ids=[second['id'],first['id']]))
        api.event('agent.tool_result',tool_use_id=first['id'])
        api.event('user.custom_tool_result',custom_tool_use_id=second['id'])
        resume = api.event('span.model_request_start')
        api.event('session.status_running')
        live = LiveSession(api,'sess-b',Path(self.folder.name)/'parallel.sqlite3')
        data = self.observe(live=live)
        entry = data['interactions']['conversations'][0]
        self.assertEqual(data['interactions']['version'],4)
        self.assertEqual([(s['source'],s['target']) for s in entry['steps'][:4]],
                         [('input','session'),('session','loop'),('loop','session'),('loop','events')])
        requests = [s for s in entry['steps'] if s['flow']=='loop-events']
        self.assertEqual(len(requests),1)
        self.assertEqual(set(requests[0]['tool_ids']),{first['id'],second['id']})
        self.assertEqual(len(requests[0]['event_ids']),2)
        resumes = [s for s in entry['steps'] if s['flow']=='events-loop']
        self.assertEqual(len(resumes),1)
        self.assertIn('qca:'+resume['id'],resumes[0]['event_ids'])

    def test_streaming_batch_grows_without_reindexing_but_next_model_round_is_separate(self):
        api = EventAPI()
        self.execution = dict(events=[], runtime=[])
        message = api.event('user.message',content=[dict(type='text',text='分析')])
        api.event('span.model_request_start')
        first = api.event('agent.tool_use',name='Read')
        live = LiveSession(api,'sess-c',Path(self.folder.name)/'stream.sqlite3')
        initial = self.observe(live=live)['interactions']['conversations'][0]
        second = api.event('agent.custom_tool_use',name='runtime_evidence')
        same = self.observe(live=live)['interactions']['conversations'][0]
        self.assertEqual(initial['total_steps'],same['total_steps'])
        request = next(s for s in same['steps'] if s['flow']=='loop-events')
        self.assertEqual(len(request['tool_ids']),2)
        api.event('user.tool_result',tool_use_id=first['id'])
        api.event('user.custom_tool_result',custom_tool_use_id=second['id'])
        api.event('span.model_request_start')
        third = api.event('agent.custom_tool_use',name='run_python')
        after = self.observe(live=live)['interactions']['conversations'][0]
        batches = [s for s in after['steps'] if s['flow']=='loop-events']
        self.assertEqual(len(batches),2)
        self.assertEqual(batches[0]['number'],request['number'])
        self.assertEqual(batches[1]['tool_ids'],[third['id']])


    def test_requires_action_can_arrive_after_multiple_streamed_calls_without_duplicate_ordinals(self):
        api = EventAPI()
        self.execution = dict(events=[],runtime=[])
        message = api.event('user.message',content=[dict(type='text',text='分析')])
        first = api.event('agent.tool_use',name='Read')
        live = LiveSession(api,'sess-d',Path(self.folder.name)/'legacy-stream.sqlite3')
        initial = self.observe(live=live)['interactions']['conversations'][0]
        second = api.event('agent.custom_tool_use',name='runtime_evidence')
        before_proof = self.observe(live=live)['interactions']['conversations'][0]
        api.event('session.status_idle',stop_reason=dict(type='requires_action',event_ids=[first['id'],second['id']]))
        after = self.observe(live=live)['interactions']['conversations'][0]
        self.assertEqual(initial['total_steps'],2)
        self.assertEqual(before_proof['total_steps'],2)
        self.assertEqual(after['total_steps'],2)
        self.assertEqual(initial['steps'][-1]['id'],after['steps'][-1]['id'])
        self.assertEqual(set(after['steps'][-1]['tool_ids']),{first['id'],second['id']})

    def test_missing_request_result_and_resume_cannot_create_a_late_first_start(self):
        api = EventAPI()
        self.execution = dict(events=[],runtime=[])
        api.event('user.message',content=[dict(type='text',text='分析')])
        api.event('user.tool_result',tool_use_id='missing-request')
        api.event('span.model_request_start')
        api.event('session.status_running')
        live = LiveSession(api,'sess-e',Path(self.folder.name)/'partial.sqlite3')
        entry = self.observe(live=live)['interactions']['conversations'][0]
        self.assertEqual([(s['source'],s['target']) for s in entry['steps']],
                         [('input','session'),('worker','events'),('events','loop')])

    def test_worker_window_rotation_does_not_erase_observed_browser_handoffs(self):
        self.api.feed[1]['name'] = 'download_report'
        self.execution['events'][0]['tool'] = 'download_report'
        self.execution['runtime'] = [dict(kind='tool_stage', time=103, tool_use_id=self.call['id'],
            stage='browser_connect',role='browser',detail='do-not-persist-stage-body'),
            dict(kind='tool_stage',time=104,tool_use_id=self.call['id'],stage='csv_transfer',role='transfer')]
        original = self.observe()
        prior = {s['id']:s['number'] for s in self.conversation(original)['steps']}
        self.execution['runtime'] = [dict(kind='heartbeat_confirmed',time=200+i,work_id='work-a') for i in range(250)]
        restarted = LiveSession(self.api,'sess-a',self.live.journal)
        after = self.observe(live=restarted)
        self.assertEqual(self.numbers(after),prior)
        restored = [e for e in after['events'] if e.get('saved_evidence')]
        self.assertEqual({e['stage'] for e in restored if e.get('stage')},{'browser_connect','csv_transfer'})
        with restarted.db() as db:
            payloads = repr(list(db.execute('SELECT payload FROM component_worker_facts')))
        self.assertNotIn('do-not-persist-stage-body',payloads)
        self.assertNotIn('heartbeat_confirmed',payloads)

    def test_saved_worker_evidence_is_scoped_to_the_selected_conversation(self):
        self.observe()
        message = self.api.event('user.message',content=[dict(type='text',text='另一条对话')])
        self.api.event('agent.message',content=[dict(type='text',text='无须工具')])
        self.live.snapshot()
        self.execution = dict(events=[],runtime=[])
        data = self.observe(session=self.live.snapshot(message_id=message['id']))
        self.assertEqual([c['message_id'] for c in data['interactions']['conversations']],[message['id']])
        self.assertFalse(any(e.get('saved_evidence') for e in data['events']))

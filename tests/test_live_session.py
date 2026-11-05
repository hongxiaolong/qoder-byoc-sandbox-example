"""Session observation, arbitrary turns, and stable terminal submission recovery."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid


from qoder_byoc_sandbox_example.demos.report_analyst.live_session import LiveSession, ChatError


class EventAPI:
    def __init__(self):
        self.feed = []
        self.receipts = {}
        self.fail_after_accept = False
        self.fail_read = False
        self.requests = []

    def events(self, session, after=None):
        if self.fail_read:
            raise RuntimeError('network failure')
        start = next((i+1 for i,e in enumerate(self.feed) if e['id'] == after), 0)
        yield from self.feed[start:]

    def event(self, kind, **values):
        event = dict(id='evt-'+str(len(self.feed)), type=kind, created_at=100+len(self.feed), **values)
        self.feed.append(event)
        return event

    def send(self, session, events, key):
        self.requests.append(key)
        if key not in self.receipts:
            event = self.event(events[0]['type'], content=events[0]['content'])
            self.receipts[key] = {'data': [event]}
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise TimeoutError('response lost after accept')
        return self.receipts[key]


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = EventAPI()
        self.path = Path(self.temp.name) / 'session.sqlite3'
        self.live = LiveSession(self.api, 'sess-a', self.path, clock=lambda: 1000)

    def turn(self, text, tool=None):
        message = self.api.event('user.message', content=[{'type':'text','text':text}])
        if tool:
            call=self.api.event('agent.custom_tool_use', name=tool, input={'private':'do-not-persist'})
            self.api.event('session.status_idle',stop_reason={'type':'requires_action','event_ids':[call['id']]})
            self.api.event('user.custom_tool_result',custom_tool_use_id=call['id'],is_error=False)
        self.api.event('agent.message',content=[{'type':'thinking','text':'private-reasoning'},{'type':'text','text':'完成 '+text}])
        self.api.event('session.status_idle',stop_reason={'type':'end_turn'})
        return message

    def test_arbitrary_console_turns_and_tool_association_survive_restart(self):
        for text in ['首次分析','连续追问','现场追加','第四轮任意问题','第五轮无需工具的问题']:
            self.turn(text, 'run_python' if text != '第五轮无需工具的问题' else None)
        result=self.live.snapshot()
        self.assertEqual(len(result['turns']),5)
        self.assertIsNone(result['active_message_id'])
        self.assertEqual(result['tools'][-1]['message_id'],result['turns'][3]['id'])
        self.assertEqual(result['tools'][-1]['state'],'posted')
        self.assertEqual(result['turns'][-1]['source'],'qoder')
        restart=LiveSession(self.api,'sess-a',self.path).snapshot()
        self.assertEqual([t['id'] for t in restart['turns']],[t['id'] for t in result['turns']])
        raw=self.path.read_bytes()
        self.assertNotIn(b'private-reasoning',raw)
        self.assertNotIn(b'do-not-persist',raw)

    def test_request_retry_reuses_receipt_and_preserves_source(self):
        key=str(uuid.uuid4())
        one=self.live.send('自由意图',key,source='terminal')
        two=self.live.send('自由意图',key,source='terminal')
        self.assertEqual(one,two)
        self.assertEqual(len(self.api.feed),1)
        result=self.live.snapshot()
        self.assertEqual(result['turns'][0]['source'],'terminal')
        self.assertEqual(result['turns'][0]['text'],'自由意图')
        with self.assertRaises(ChatError):
            self.live.send('换个内容',key)
        with self.assertRaises(ChatError):
            self.live.send('另一条消息',str(uuid.uuid4()))

    def test_lost_send_response_can_be_retried_after_restart_without_duplicate(self):
        key=str(uuid.uuid4())
        self.api.fail_after_accept=True
        with self.assertRaises(TimeoutError):
            self.live.send('网络重试',key)
        restart=LiveSession(self.api,'sess-a',self.path)
        receipt=restart.send('网络重试',key)
        self.assertEqual(receipt['id'],self.api.feed[0]['id'])
        self.assertEqual(len(self.api.feed),1)
        self.assertEqual(self.api.requests,[self.api.requests[0]]*2)

    def test_read_failure_keeps_history_and_marks_it_stale(self):
        self.turn('已完成的问题')
        self.live.snapshot()
        self.api.fail_read=True
        result=self.live.snapshot()
        self.assertTrue(result['stale'])
        self.assertEqual(result['turns'][0]['text'],'已完成的问题')

    def test_local_send_lock_does_not_allow_two_processes_to_submit(self):
        other=LiveSession(self.api,'sess-a',self.path)
        with self.live.locked():
            with self.assertRaises(ChatError):
                other.send('并发发送',str(uuid.uuid4()))
        self.assertEqual(self.api.feed,[])

    def test_interrupt_and_termination_are_not_success(self):
        self.api.event('user.message',content=[{'type':'text','text':'停止任务'}])
        self.api.event('user.interrupt')
        self.assertEqual(self.live.snapshot()['turns'][0]['state'],'interrupted')
        self.api.event('session.status_terminated')
        with self.assertRaises(ChatError) as error:
            self.live.send('再发一条',str(uuid.uuid4()))
        self.assertEqual(error.exception.status,410)

    def test_official_times_model_spans_and_statuses_are_public_only(self):
        message = self.api.event('user.message', content=[{'type':'text','text':'分析数据'}])
        started = self.api.event('span.model_request_start', processed_at='2026-10-30T01:00:00Z',
                                 input={'secret':'hidden-model-input'}, reasoning='hidden-reasoning')
        ended = self.api.event('span.model_request_end', processed_at='2026-10-30T01:00:02Z',
                               model_request_start_id=started['id'], output={'private':'hidden-model-output'})
        self.api.event('session.status_idle', stop_reason={'type':'requires_action','event_ids':[]})
        self.api.event('session.status_running')
        self.api.event('session.status_rescheduling')
        result = self.live.snapshot()
        trace = {item['id']:item for item in result['trace']}
        self.assertEqual(trace[ended['id']]['time'] - trace[started['id']]['time'], 2)
        self.assertEqual(trace[ended['id']]['model_request_start_id'], started['id'])
        self.assertEqual(trace[ended['id']]['message_id'], message['id'])
        self.assertEqual(result['turns'][0]['state'], 'running')
        self.assertIn('session.status_rescheduling', [item['type'] for item in result['trace']])
        raw = self.path.read_bytes()
        for value in (b'hidden-model-input',b'hidden-reasoning',b'hidden-model-output'):
            self.assertNotIn(value,raw)
            self.assertNotIn(value.decode(),json.dumps(result))

    def test_long_turn_hydrates_old_root_reply_and_tool_request(self):
        root = self.api.event('user.message', content=[{'type':'text','text':'很长的任务'}])
        call = self.api.event('agent.custom_tool_use', name='run_python', input={'private':'never-persist'})
        self.api.event('agent.message',content=[{'type':'text','text':'较早的公开回复'}])
        for _ in range(1205):
            self.api.event('span.model_request_start')
        self.api.event('user.custom_tool_result',custom_tool_use_id=call['id'],is_error=False)
        result = self.live.snapshot()
        self.assertTrue(result['history_incomplete'])
        self.assertEqual(result['turns'][0]['id'],root['id'])
        self.assertEqual(result['turns'][0]['replies'][0]['text'],'较早的公开回复')
        self.assertEqual(result['tools'][0]['id'],call['id'])
        self.assertEqual(result['tools'][0]['state'],'posted')
        self.api.event('session.status_idle',stop_reason={'type':'end_turn'})
        completed = self.live.snapshot()
        self.assertEqual(completed['turns'][0]['state'],'completed')
        self.assertEqual(completed['turns'][0]['replies'][0]['text'],'较早的公开回复')
        self.assertEqual(completed['tools'][0]['state'],'posted')

    def test_send_defaults_to_terminal_source(self):
        self.live.send('默认终端消息',str(uuid.uuid4()))
        self.assertEqual(self.live.snapshot()['turns'][0]['source'],'terminal')

    def test_rescheduling_reopens_latest_turn_and_blocks_new_submission(self):
        message = self.turn('需要重新调度的任务')
        self.api.event('session.status_rescheduling',message_id=message['id'])
        result = self.live.snapshot()
        self.assertEqual(result['active_message_id'],message['id'])
        self.assertEqual(result['turns'][0]['state'],'running')
        with self.assertRaises(ChatError):
            self.live.send('不能并发提交',str(uuid.uuid4()))

    def test_unbound_session_status_before_new_input_never_reopens_terminal_turn(self):
        for ending,state in [('end_turn','completed'),('max_turns','stopped'),('interrupt','interrupted')]:
            with self.subTest(ending=ending):
                message=self.api.event('user.message',content=[{'type':'text','text':'已结束的任务'}])
                if ending=='interrupt':
                    self.api.event('user.interrupt')
                else:
                    self.api.event('session.status_idle',stop_reason={'type':ending})
                statuses=[self.api.event(kind) for kind in ('session.status_running','session.status_busy','session.status_rescheduling')]
                result=self.live.snapshot()
                self.assertEqual(result['turns'][-1]['state'],state)
                self.assertIsNone(result['active_message_id'])
                trace={event['id']:event for event in result['trace']}
                self.assertTrue(all(trace[event['id']]['message_id'] is None for event in statuses))
                newer=self.api.event('user.message',content=[{'type':'text','text':'新的输入'}])
                updated=self.live.snapshot()
                self.assertEqual(updated['active_message_id'],newer['id'])
                self.assertEqual(next(turn for turn in updated['turns'] if turn['id']==message['id'])['state'],state)

    def test_legacy_misattributed_running_is_corrected_only_in_projection(self):
        message=self.turn('旧日志已完成任务')
        self.live.snapshot()
        legacy=dict(id='legacy-running',type='session.status_running',message_id=message['id'],time=999,observed_at=1000)
        with self.live.db() as db:
            raw=json.dumps(legacy)
            db.execute('INSERT INTO events(id,payload) VALUES (?,?)',(legacy['id'],raw))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('active_message_id',?)",(message['id'],))
        result=self.live.snapshot(refresh=False)
        self.assertEqual(result['turns'][0]['state'],'completed')
        self.assertIsNone(result['active_message_id'])
        self.assertIsNone(next(event for event in result['trace'] if event['id']==legacy['id'])['message_id'])
        replay=self.live.snapshot(refresh=False,message_id=message['id'])
        self.assertEqual(replay['turns'][0]['state'],'completed')
        with self.live.db() as db:
            self.assertEqual(db.execute('SELECT payload FROM events WHERE id=?',(legacy['id'],)).fetchone()[0],raw)
        self.api.event('session.status_running')
        refreshed=self.live.snapshot()
        self.assertEqual(refreshed['turns'][0]['state'],'completed')
        self.assertIsNone(refreshed['active_message_id'])

    def test_selected_old_message_replays_beyond_default_window_without_new_active_root(self):
        message=self.turn('旧消息完整历史',tool='Read')
        for index in range(1250):
            self.api.event('span.model_request_start',message_id=message['id'])
        newer=self.api.event('user.message',content=[{'type':'text','text':'另一条新任务'}])
        for index in range(1250):
            self.api.event('span.model_request_start',message_id=newer['id'])
        live=self.live.snapshot()
        self.assertNotIn(message['id'],[turn['id'] for turn in live['turns']])
        replay=self.live.snapshot(refresh=False,message_id=message['id'])
        self.assertEqual([turn['id'] for turn in replay['turns']],[message['id']])
        self.assertGreater(len(replay['trace']),1200)
        self.assertTrue(all(event['message_id']==message['id'] for event in replay['trace']))
        self.assertFalse(replay['history_incomplete'])
        self.assertEqual(self.api.requests,[])

    def test_selected_history_rejects_invalid_unknown_and_non_message_ids(self):
        message=self.turn('只读历史',tool='Read')
        self.live.snapshot()
        tool=next(event for event in self.api.feed if event['type']=='agent.custom_tool_use')
        for value,status in [('',400),('../private',400),('not-recorded',404),(tool['id'],404)]:
            with self.subTest(value=value):
                with self.assertRaises(ChatError) as error:
                    self.live.snapshot(refresh=False,message_id=value)
                self.assertEqual(error.exception.status,status)
        self.assertEqual(self.live.snapshot(refresh=False,message_id=message['id'])['turns'][0]['text'],'只读历史')
        self.assertEqual(self.api.requests,[])

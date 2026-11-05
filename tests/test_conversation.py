"""Exercise actual terminal chat and durable retries against a local API fake."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from qoder_byoc_sandbox_example.demos.report_analyst import conversation
from qoder_byoc_sandbox_example.demos.report_analyst.live_session import ChatError, LiveSession


class AcceptThenLoseResponseAPI:
    def __init__(self):
        self.feed, self.receipts, self.requests = [], {}, []
        self.lose_response = True

    def events(self, session, after=None):
        start = next((index + 1 for index, event in enumerate(self.feed) if event['id'] == after), 0)
        yield from self.feed[start:]

    def send(self, session, events, key):
        self.requests.append((key, events))
        if key not in self.receipts:
            event = dict(events[0],id='msg-' + str(len(self.feed)),processed_at=time.time())
            self.feed.append(event)
            self.receipts[key] = {'data':[event]}
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('accepted but response was lost')
        return self.receipts[key]


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = AcceptThenLoseResponseAPI()
        self.config = {'sandbox':{'timeout_seconds':600},'demo':{'turn_timeout_seconds':0}}
        self.state = dict(run_id='run-a',session_id='session-a',created_at=time.time(),
                          collect_verified=True,revise_verified=True,append_verified=True)
        self.addCleanup(patch.stopall)
        patch.object(conversation,'results',return_value=self.root).start()
        patch.object(conversation,'observer',side_effect=lambda *_: LiveSession(
            self.api,self.state['session_id'],self.root/'live-session.sqlite3')).start()
        self.output = io.StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__,None,None,None)

    @property
    def pending(self):
        return self.root/'pending-message.json'

    def test_actual_chat_retry_after_restart_keeps_private_pending_uuid_and_text(self):
        with self.assertRaisesRegex(RuntimeError,'chat --retry'):
            conversation.chat(self.config,{},self.state,message='恢复原消息')
        pending = json.loads(self.pending.read_text())
        self.assertEqual(pending['text'],'恢复原消息')
        self.assertEqual(self.pending.stat().st_mode & 0o777,0o600)
        with self.assertRaisesRegex(ChatError,'尚未确认'):
            conversation.chat(self.config,{},self.state,message='不能覆盖的新消息')
        self.assertEqual(json.loads(self.pending.read_text()),pending)
        self.assertEqual(len(self.api.requests),1)
        conversation.chat(self.config,{},self.state,retry=True)
        self.assertFalse(self.pending.exists())
        self.assertEqual(len(self.api.feed),1)
        self.assertEqual(self.api.requests[0],self.api.requests[1])
        self.assertEqual(self.api.requests[0][0],'live-'+pending['request_id'])
        live = LiveSession(self.api,'session-a',self.root/'live-session.sqlite3')
        self.assertEqual(live.snapshot()['turns'][0]['source'],'terminal')

    def test_interactive_failure_stays_in_chat_and_retry_uses_same_request(self):
        with patch('builtins.input',side_effect=['现场追问','/retry','/exit']):
            conversation.chat(self.config,{},self.state)
        self.assertEqual(len(self.api.feed),1)
        self.assertEqual(self.api.requests[0],self.api.requests[1])
        self.assertFalse(self.pending.exists())

    def test_explicit_discard_clears_record_without_resending(self):
        with patch('builtins.input',side_effect=['现场追问','另一个请求','/discard','/exit']):
            conversation.chat(self.config,{},self.state)
        self.assertEqual(len(self.api.requests),1)
        self.assertFalse(self.pending.exists())
        self.assertIn('不会撤回',self.output.getvalue())

    def test_retry_requires_pending_and_message_cannot_be_combined(self):
        with self.assertRaisesRegex(ChatError,'没有待确认'):
            conversation.chat(self.config,{},self.state,retry=True)
        with self.assertRaisesRegex(ValueError,'不能同时'):
            conversation.chat(self.config,{},self.state,message='new',retry=True)
        self.assertEqual(self.api.requests,[])

    def test_failed_atomic_record_write_never_sends(self):
        with patch.object(conversation.os,'replace',side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(ChatError,'消息未提交'):
                conversation.chat(self.config,{},self.state,message='cannot persist')
        self.assertEqual(self.api.requests,[])
        self.assertFalse(self.pending.exists())
        self.assertEqual(list(self.root.glob('.pending-*')),[])

    def test_empty_single_message_is_an_error(self):
        with self.assertRaises(ChatError):
            conversation.chat(self.config,{},self.state,message='')
        self.assertEqual(self.api.requests,[])


    def test_one_shot_exit_text_is_sent_as_message_not_local_command(self):
        self.api.lose_response = False
        conversation.chat(self.config,{},self.state,message='/exit')
        self.assertEqual(self.api.feed[0]['content'][0]['text'],'/exit')
        self.assertFalse(self.pending.exists())

    def test_exit_during_submission_does_not_clear_the_saved_retry_record(self):
        import threading
        stopped = threading.Event()
        class CompletingAfterExit:
            def send(_self, text, key, source):
                stopped.set()
                return {'id':'message-after-exit'}
        receipt = conversation.submit(CompletingAfterExit(),self.state,'pending text',stopped=stopped)
        self.assertEqual(receipt['id'],'message-after-exit')
        self.assertEqual(json.loads(self.pending.read_text())['text'],'pending text')

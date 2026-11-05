"""Real terminal commands while chat's local network operations are blocked."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest


CHILD = r'''
import json,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
from qoder_byoc_sandbox_example.demos.report_analyst import conversation
root=Path(sys.argv[1]);mode=sys.argv[2]
class Live:
    client=SimpleNamespace()
    def send(self,text,key,source):
        with (root/'requests.jsonl').open('a') as stream:
            stream.write(json.dumps({'text':text,'key':key})+'\n')
        if mode=='submission':
            print('SUBMISSION_BLOCKED',flush=True)
            threading.Event().wait(60)
        return {'id':'message-a'}
    def snapshot(self):
        if mode=='observation':
            print('OBSERVATION_BLOCKED',flush=True)
            threading.Event().wait(60)
        return {'turns':[{'id':'message-a','state':'completed','replies':[{'text':'finished'}]}],
                'tools':[],'terminal':False}
conversation.results=lambda _:root
conversation.observer=lambda *_:Live()
conversation.bind_trace=lambda *args,**kw:None
state={'run_id':'run-a','session_id':'session-a','created_at':time.time(),
       'collect_verified':True,'revise_verified':True,'append_verified':True}
config={'sandbox':{'timeout_seconds':600},'demo':{'turn_timeout_seconds':60}}
conversation.chat(config,{},state)
print('PARENT_MENU',flush=True)
print('MENU_CHOICE='+input(),flush=True)
'''


@unittest.skipUnless(os.name == 'posix', 'terminal polling uses POSIX file descriptors')
class ChatTerminalTests(unittest.TestCase):
    def launch(self, mode):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        script = root/'chat.py'
        script.write_text(CHILD)
        master, slave = pty.openpty()
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]/'src')
        child = subprocess.Popen([sys.executable, '-u', str(script), str(root), mode],
                                 stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        def close():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            os.close(master)
        self.addCleanup(close)
        output = bytearray()
        def until(text, timeout=4):
            expected = text.encode()
            deadline = time.monotonic()+timeout
            while expected not in output and time.monotonic()<deadline:
                if select.select([master],[],[],0.05)[0]:
                    try:
                        value = os.read(master,65536)
                    except OSError:
                        break
                    if not value:
                        break
                    output.extend(value)
            self.assertIn(expected,output,output.decode(errors='replace'))
        until('你 ›')
        return root, master, child, until, output

    def test_only_slash_exit_returns_without_submitting_any_exit_message(self):
        root, terminal, child, until, output = self.launch('complete')
        os.write(terminal,b'/back\n')
        until('未知指令')
        self.assertFalse((root/'requests.jsonl').exists())
        os.write(terminal,b'/exit\nview\n')
        until('MENU_CHOICE=view')
        self.assertEqual(child.wait(timeout=2),0)
        self.assertFalse((root/'requests.jsonl').exists())
        self.assertIn('输入 /exit 结束 chat 返回主菜单'.encode(),output)

    def test_exit_during_blocked_submission_preserves_pending_and_menu_input(self):
        root, terminal, child, until, _ = self.launch('submission')
        os.write(terminal,'分析数据\n'.encode())
        until('SUBMISSION_BLOCKED')
        pending = json.loads((root/'pending-message.json').read_text())
        started = time.monotonic()
        os.write(terminal,b'/exit\ncleanup\n')
        until('MENU_CHOICE=cleanup')
        self.assertEqual(child.wait(timeout=2),0)
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual(json.loads((root/'pending-message.json').read_text()),pending)
        self.assertEqual([json.loads(line)['text'] for line in (root/'requests.jsonl').read_text().splitlines()],['分析数据'])

    def test_exit_during_blocked_observation_leaves_cloud_message_and_menu_input(self):
        root, terminal, child, until, _ = self.launch('observation')
        os.write(terminal,'分析数据\n'.encode())
        until('OBSERVATION_BLOCKED')
        self.assertFalse((root/'pending-message.json').exists())
        os.write(terminal,b'/exit\nview\n')
        until('MENU_CHOICE=view')
        self.assertEqual(child.wait(timeout=2),0)
        self.assertEqual([json.loads(line)['text'] for line in (root/'requests.jsonl').read_text().splitlines()],['分析数据'])

    def test_bare_exit_remains_message_text(self):
        root, terminal, child, until, _ = self.launch('complete')
        os.write(terminal,b'exit\n')
        until('finished')
        os.write(terminal,b'/exit\nview\n')
        until('MENU_CHOICE=view')
        self.assertEqual(child.wait(timeout=2),0)
        self.assertEqual([json.loads(line)['text'] for line in (root/'requests.jsonl').read_text().splitlines()],['exit'])

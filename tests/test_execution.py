import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from qoder_byoc_sandbox_example.demos.report_analyst.execution import execution_snapshot
from qoder_byoc_sandbox_example.demos.report_analyst.service import create_server


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'worker.jsonl'
        (self.root / 'identity.json').write_text(json.dumps(dict(qoder_session_url='https://qoder.com/cloud/sessions/sess-test',browser_sandbox_id='browser-test',analysis_sandbox_id='analysis-test')))

    def event(self, kind, tool, key='python', **extra):
        with self.log.open('a') as out:
            out.write(json.dumps(dict(kind=kind,tool=tool,tool_use_id=key,session_id='sess-test',time=1,**extra))+'\n')

    def test_real_started_code_and_finished_stdout_are_joined(self):
        self.event('tool_start','run_python',input={'code':"print('actual')"})
        event = execution_snapshot(self.root,self.log)['events'][0]
        self.assertEqual(event['state'],'running')
        self.assertEqual(event['code'],"print('actual')")
        self.event('tool_finished','run_python',output='exit_code=0\nactual',is_error=False,elapsed_ms=12)
        event = execution_snapshot(self.root,self.log)['events'][0]
        self.assertEqual(event['output'],'exit_code=0\nactual')
        self.assertEqual(event['state'],'completed')

    def test_browser_execution_and_transfer_are_distinguished(self):
        self.event('tool_start','download_report',input={'month':'2026-10','region':'华东','token':'hidden'})
        self.event('tool_finished','download_report',output=json.dumps({'rows':100,'bytes':3360,'browser_path':'/browser/data.csv','analysis_path':'/workspace/input.csv','token':'hidden'}))
        data = execution_snapshot(self.root,self.log)
        self.assertEqual(data['events'][0]['role'],'browser')
        self.assertIn('100',data['events'][0]['output_summary'])
        self.assertNotIn('hidden',json.dumps(data))

    def test_other_sessions_generic_tools_partial_lines_and_secrets_not_exposed(self):
        self.event('tool_start','Bash',input={'command':'secret-command'})
        self.event('tool_start','run_python',input={'code':'API_KEY="private-key"\nprint(1)'})
        self.event('tool_finished','run_python',output='Bearer private-token https://host/upload?_state=private-state',is_error=True)
        with self.log.open('a') as out:
            out.write(json.dumps(dict(kind='tool_start',tool='run_python',tool_use_id='other',session_id='another-session',input={'code':'other-session'}))+'\nnull\n{"partial":')
        data=execution_snapshot(self.root,self.log)
        text=json.dumps(data)
        for secret in ['secret-command','private-key','private-token','private-state','other-session']:
            self.assertNotIn(secret,text)
        self.assertEqual(data['events'][0]['state'],'error')

    def test_http_endpoint_and_missing_log_are_honest(self):
        server=create_server(self.root,0,'127.0.0.1',execution_log=self.log)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,3);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/execution') as response:
            self.assertEqual(json.load(response)['events'],[])
        self.event('tool_start','append_record',input={'amount':6666,'category':'现场演示'})
        self.event('tool_finished','append_record',output=json.dumps({'before_rows':100,'after_rows':101,'record':{'amount':6666}}))
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/execution') as response:
            event=json.load(response)['events'][0]
            self.assertIn('100 → 101',event['output_summary'])
            self.assertIn('不含表头',event['output_summary'])

    def test_observed_started_finished_and_posted_are_distinct(self):
        self.event('tool_observed','run_python',message_id='message-4')
        first=execution_snapshot(self.root,self.log)['events'][0]
        self.assertEqual(first['state'],'observed')
        self.assertNotIn('started_at',first)
        self.event('tool_start','run_python',input={'code':'print(42)'},message_id='message-4')
        self.event('tool_finished','run_python',output='exit_code=0\n42',elapsed_ms=23)
        finished=execution_snapshot(self.root,self.log)['events'][0]
        self.assertEqual(finished['message_id'],'message-4')
        self.assertEqual(finished['elapsed_ms'],23)
        self.assertNotIn('posted_at',finished)
        self.event('tool_posted','run_python')
        self.assertEqual(execution_snapshot(self.root,self.log)['events'][0]['posted_at'],1)

    def test_lease_and_tool_stages_expose_only_observed_public_fields(self):
        self.event('work_claimed',None,work_id='work-1',secret='must-not-leak')
        self.event('heartbeat_confirmed',None,work_id='work-1',secret='must-not-leak')
        self.event('tool_stage','download_report',stage='csv_transfer',role='transfer',detail='CSV 已传输',secret='must-not-leak')
        data=execution_snapshot(self.root,self.log)
        self.assertEqual([e['kind'] for e in data['runtime']],['work_claimed','heartbeat_confirmed','tool_stage'])
        self.assertEqual(data['runtime'][-1]['role'],'transfer')
        self.assertNotIn('must-not-leak',json.dumps(data))

    def test_read_skill_and_demo_files_are_visible_but_unknown_reads_are_hidden(self):
        self.event('tool_start', 'Read', key='skill', input={'file_path':'/workspace/skills/report-analysis/SKILL.md'})
        self.event('tool_finished', 'Read', key='skill', output='1: # Report analysis\n2: Read the CSV first.')
        self.event('tool_start', 'Read', key='demo', input={'file_path':'input.csv', 'limit':10})
        self.event('tool_finished', 'Read', key='demo', output='1: date,category,amount')
        self.event('tool_start', 'Read', key='private', input={'file_path':'/workspace/private/settings.json'})
        self.event('tool_finished', 'Read', key='private', output='private-file-contents')
        self.event('tool_start', 'Bash', key='command', input={'command':'private-command'})
        events = execution_snapshot(self.root,self.log)['events']
        self.assertEqual(events[0]['read_path'],'/workspace/skills/report-analysis/SKILL.md')
        self.assertEqual(events[0]['title'],'读取 Skill')
        self.assertIn('Read the CSV first.',events[0]['output'])
        self.assertIn('/workspace/input.csv',events[1]['input'])
        self.assertIn('date,category,amount',events[1]['output'])
        text = json.dumps(events)
        for value in ['private-file-contents','private/settings.json','private-command']:
            self.assertNotIn(value,text)

    def test_read_skill_symlink_cannot_expose_an_unapproved_file(self):
        private = self.root / 'private/settings.json'
        private.parent.mkdir()
        private.write_text('private-symlink-content')
        skill = self.root / 'skills/report/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.symlink_to(private)
        self.event('tool_start','Read',input={'file_path':'/workspace/skills/report/SKILL.md'})
        self.event('tool_finished','Read',output=private.read_text())
        self.assertNotIn('private-symlink-content',json.dumps(execution_snapshot(self.root,self.log)))

    def test_known_credentials_url_userinfo_and_quoted_comma_secrets_are_redacted(self):
        self.event('tool_start','run_python',input={'code':'print("opaque-environment-credential")'})
        self.event('tool_finished','run_python',output='opaque-environment-credential\npassword="alpha,beta"\n{"api_key":"gamma,delta"}\nhttps://reader:private-pass@example.test/path?token=private-query')
        with patch.dict('os.environ',{'QODER_ACCESS_TOKEN':'opaque-environment-credential'}):
            data = json.dumps(execution_snapshot(self.root,self.log))
        for private in ['opaque-environment-credential','alpha','beta','gamma','delta','reader','private-pass','private-query']:
            self.assertNotIn(private,data)
        self.assertIn('example.test/path',data)

    def test_tail_retains_orphan_finished_and_posted_without_invented_start_time(self):
        self.event('tool_start','run_python',input={'code':'print(1)'})
        with self.log.open('a') as out:
            out.write('x' * (2 * 1024 * 1024 + 100) + '\n')
        self.event('tool_finished','run_python',output='exit_code=0\n1',elapsed_ms=20)
        self.event('tool_posted','run_python')
        data = execution_snapshot(self.root,self.log)
        self.assertTrue(data['truncated'])
        self.assertTrue(data['history_incomplete'])
        event = data['events'][0]
        self.assertTrue(event['history_incomplete'])
        self.assertEqual(event['state'],'completed')
        self.assertEqual(event['posted_at'],1)
        self.assertNotIn('started_at',event)
        self.assertNotIn('observed_at',event)

    def test_poll_ack_heartbeat_and_cancellation_preserve_observed_state(self):
        self.event('work_poll',None,result='returned',state='queued',work_id='work-1')
        self.event('work_ack',None,state='starting',work_id='work-1')
        self.event('heartbeat_confirmed',None,state='active',ttl_seconds=45,last_heartbeat='2026-10-30T01:02:03Z')
        self.event('tool_start','run_python',input={'code':'print(1)'})
        self.event('tool_cancelled','run_python')
        data = execution_snapshot(self.root,self.log)
        self.assertEqual(data['runtime'][0]['state'],'queued')
        self.assertEqual(data['runtime'][1]['state'],'starting')
        self.assertEqual(data['runtime'][2]['ttl_seconds'],45)
        self.assertEqual(data['runtime'][2]['last_heartbeat'],'2026-10-30T01:02:03Z')
        self.assertEqual(data['events'][0]['state'],'cancelled')
        self.assertNotIn('posted_at',data['events'][0])

    def test_tool_span_keeps_recorded_work_without_using_current_work(self):
        self.event('tool_observed','run_python',work_id='work-old')
        self.event('tool_start','run_python',input={'code':'print(1)'},work_id='work-old')
        self.event('tool_finished','run_python',output='exit_code=0\n1',work_id='work-old')
        self.event('work_claimed',None,work_id='work-new')
        self.event('tool_posted','run_python',work_id='work-new')
        self.event('tool_start','Read',key='without-work',input={'file_path':'/workspace/input.csv'})
        events=execution_snapshot(self.root,self.log)['events']
        self.assertEqual(events[0]['work_id'],'work-old')
        self.assertIsNone(events[1]['work_id'])

    def test_orphan_tool_record_preserves_its_recorded_work(self):
        self.event('tool_finished','run_python',output='exit_code=0\n1',work_id='work-orphan')
        event=execution_snapshot(self.root,self.log)['events'][0]
        self.assertEqual(event['work_id'],'work-orphan')
        self.assertTrue(event['history_incomplete'])

    def test_explicit_identity_projects_saved_log_without_workspace_identity_file(self):
        self.event('tool_start','run_python',input={'code':'print("saved")'})
        self.event('tool_finished','run_python',output='exit_code=0\nsaved')
        self.event('tool_start','Bash',key='generic',input={'command':'private-command'})
        self.event('tool_finished','Bash',key='generic',output='private-generic-output')
        identity=json.loads((self.root/'identity.json').read_text())
        (self.root/'identity.json').unlink()
        data=execution_snapshot(self.root,self.log,identity=identity)
        self.assertEqual(data['events'][0]['code'],'print("saved")')
        self.assertIn('saved',data['events'][0]['output'])
        self.assertNotIn('private-command',json.dumps(data))
        self.assertNotIn('private-generic-output',json.dumps(data))
        other=execution_snapshot(self.root,self.log,identity={'qoder_session_url':'https://qoder.com/cloud/sessions/another-session'})
        self.assertEqual(other['events'],[])

    def test_copied_raw_log_does_not_prove_skill_read_content_is_public(self):
        self.event('tool_start','Read',input={'file_path':'/workspace/skills/report/SKILL.md'})
        self.event('tool_finished','Read',output='confidential historical document with no credential-shaped words')
        identity=json.loads((self.root/'identity.json').read_text())
        (self.root/'identity.json').unlink()
        copied=execution_snapshot(self.root,self.log,identity=identity,allow_read_content=False)
        event=copied['events'][0]
        self.assertNotIn('confidential historical document',json.dumps(copied))
        self.assertNotIn('SKILL.md',event['input'])
        self.assertNotIn('read_path',event)
        self.assertEqual(event['state'],'completed')
        live=execution_snapshot(self.root,self.log,identity=identity)
        self.assertIn('confidential historical document',live['events'][0]['output'])

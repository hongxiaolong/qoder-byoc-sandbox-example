"""Real loopback integration: QCA fixture → observer → strictly read-only UI."""
from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from test_live_session import EventAPI

from qoder_byoc_sandbox_example.demos.report_analyst.live_session import LiveSession
from qoder_byoc_sandbox_example.demos.report_analyst.execution import execution_snapshot
from qoder_byoc_sandbox_example.demos.report_analyst.observation import WorkObserver
from qoder_byoc_sandbox_example.demos.report_analyst.preview import create_app
from qoder_byoc_sandbox_example.demos.report_analyst.service import create_server


class PreviewObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "worker.jsonl"
        self.now = time.time()
        self.api = EventAPI()
        self.message = self.api.event("user.message", content=[{"type":"text", "text":"请阅读技能，再解释现有数据。"}])
        self.call = self.api.event("agent.tool_use", name="Read", input={"file_path":"/workspace/skills/demo/SKILL.md"})
        (self.root / "identity.json").write_text(json.dumps({
            "run_id":"run-test", "qoder_session_url":"https://qoder.com/cloud/sessions/sess-a",
            "analysis_sandbox_id":"sbx-analysis", "browser_sandbox_id":"sbx-browser"}))
        self.record("work_claimed", work_id="work-a")
        self.record("heartbeat_confirmed", work_id="work-a", ttl_seconds=60)
        self.record("tool_start", tool="Read", tool_use_id=self.call["id"], message_id=self.message["id"],
                    work_id="work-a", input={"file_path":"/workspace/skills/demo/SKILL.md"})
        self.record("tool_finished", tool="Read", tool_use_id=self.call["id"], message_id=self.message["id"],
                    work_id="work-a", output="Use existing input.csv; do not append without a request.", elapsed_ms=7)
        self.record("tool_posted", tool="Read", tool_use_id=self.call["id"], work_id="work-a")
        self.api.event("user.tool_result", tool_use_id=self.call["id"])
        self.api.event("agent.message", content=[{"type":"text","text":"本次只解释，没有执行追加。"}])
        self.api.event("session.status_idle", stop_reason={"type":"end_turn"})
        self.upstream = create_server(self.root, 0, "127.0.0.1", execution_log=self.log)
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        holder = self
        self.status_overrides = {}
        class Status:
            def snapshot(self):
                return {"state":"ready", "run_id":"run-test", "session_id":"sess-a", "agent_id":"agent-a",
                        "agent_version":3, "environment_id":"env-a", "skill_id":"skill-a",
                        "analysis_sandbox_id":"sbx-analysis", "browser_sandbox_id":"sbx-browser",
                        "session_url":"https://qoder.com/cloud/sessions/sess-a",
                        "phases":{"collect":{"message_id":holder.message["id"],"state":"verified"}},
                        **holder.status_overrides}
        class QueueAPI:
            def request(self, method, path):
                if path.endswith("/stats"):
                    return {"type":"work_queue_stats","depth":0,"pending":0,"workers_polling":0,"oldest_queued_at":None}
                return {"id":"work-a","type":"work","environment_id":"env-a",
                        "data":{"type":"session","id":"sess-a"},"state":"active",
                        "latest_heartbeat_at":datetime.now(timezone.utc).isoformat(),
                        "secret":"private-work-secret","metadata":{"hidden":"private-metadata"}}
        self.live = LiveSession(self.api, "sess-a", self.root/"events.sqlite3")
        self.status_provider=Status()
        app = await create_app({"analysis_url":f"http://127.0.0.1:{self.upstream.server_port}",
                                "analysis_token":"private-upstream-token","sandbox_region":"cn-hangzhou"},
                               self.status_provider, self.live, work_observer=WorkObserver(QueueAPI(),"env-a","sess-a"))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    def record(self, kind, **extra):
        with self.log.open("a") as stream:
            stream.write(json.dumps({"kind":kind,"session_id":"sess-a","time":time.time(),**extra})+"\n")

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.to_thread(self.upstream.shutdown)
        self.upstream.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    async def test_observer_joins_real_execution_and_keeps_queue_separate(self):
        response = await self.client.get("/api/observer")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["version"],1)
        self.assertEqual(data["session"]["turns"][0]["source"],"start.sh")
        self.assertEqual(data["worker"]["state"],"healthy")
        self.assertEqual(data["queue"]["stats"]["workers_polling"],0)
        self.assertEqual(data["queue"]["work"]["state"],"active")
        self.assertEqual(data["status"]["sandbox_region"],"cn-hangzhou")
        execution = next(e for e in data["events"] if e["kind"]=="tool_execution")
        self.assertIn("SKILL.md",execution["input"])
        self.assertIn("Use existing",execution["output"])
        self.assertEqual(execution["elapsed_ms"],7)
        self.assertEqual(execution["work_id"],"work-a")
        serialized=json.dumps(data)
        for private in ("private-upstream-token","private-work-secret","private-metadata"):
            self.assertNotIn(private,serialized)

    async def test_no_web_write_or_legacy_report_route(self):
        response=await self.client.post("/api/messages",json={"text":"must not be sent"})
        self.assertEqual(response.status,405)
        for path in ("/api/chat-token","/dashboard","/files/analysis.xlsx","/source","/assets/dashboard.html","/assets/source.html"):
            self.assertEqual((await self.client.get(path)).status,404,path)
        self.assertEqual(self.api.requests,[])

    async def test_console_followup_is_visible_without_tools(self):
        self.live.snapshot()
        self.api.event("user.message",content=[{"type":"text","text":"再解释一下，不要操作文件"}])
        self.api.event("agent.message",content=[{"type":"text","text":"这是无需工具的解释"}])
        self.api.event("session.status_idle",stop_reason={"type":"end_turn"})
        response=await self.client.get("/api/session")
        data=await response.json()
        self.assertEqual(len(data["turns"]),2)
        self.assertEqual(data["turns"][-1]["replies"][0]["text"],"这是无需工具的解释")
        self.assertEqual(len(data["tools"]),1)

    async def test_local_identity_still_supports_wizard_reuse(self):
        data=await (await self.client.get("/api/identity")).json()
        self.assertEqual(data,{"run_id":"run-test","analysis_sandbox_id":"sbx-analysis","browser_sandbox_id":"sbx-browser"})

    async def test_old_preview_cannot_claim_new_run_identity(self):
        self.status_overrides.update(state="unavailable", run_id="new-run",
                                     analysis_sandbox_id="new-analysis", browser_sandbox_id="new-browser")
        response = await self.client.get("/api/identity")
        self.assertEqual(response.status, 409)
        self.assertNotIn("new-run", await response.text())

    async def test_assets_exist_and_foreign_hosts_are_rejected(self):
        page=await self.client.get("/")
        html=await page.text()
        self.assertNotIn("<textarea",html)
        self.assertNotIn("data-phase=",html)
        for path in set(re.findall(r'(?:src|href)="(/assets/[^"]+)"',html)):
            self.assertEqual((await self.client.get(path)).status,200,path)
        self.assertEqual((await self.client.get("/assets/icons/file-code.svg")).status,200)
        self.assertEqual((await self.client.get("/api/session",headers={"Host":"foreign.example"})).status,403)
        self.assertEqual((await self.client.get("/assets/../../config/.env")).status,404)

    async def test_worker_failure_does_not_invent_success_or_zero_counters(self):
        self.record("heartbeat_failed",work_id="work-a")
        data=await (await self.client.get("/api/observer")).json()
        self.assertEqual(data["worker"]["state"],"stopped")
        self.assertEqual(data["queue"]["stats"]["depth"],0)
        self.assertTrue(any(e["kind"]=="heartbeat_failed" for e in data["events"]))

    async def test_missing_execution_keeps_public_session_history(self):
        self.log.unlink()
        data=await (await self.client.get("/api/observer")).json()
        self.assertEqual(data["worker"]["state"],"unknown")
        self.assertEqual(len(data["session"]["turns"]),1)
        self.assertFalse(any(e["kind"]=="tool_execution" for e in data["events"]))

    async def test_failed_execution_read_stays_stale_during_cache_window(self):
        initial = await (await self.client.get("/api/execution")).json()
        self.assertFalse(initial["stale"])
        await asyncio.to_thread(self.upstream.shutdown)
        await asyncio.sleep(1.01)
        failed = await (await self.client.get("/api/execution")).json()
        cached = await (await self.client.get("/api/execution")).json()
        self.assertTrue(failed["stale"])
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["observed_at"], initial["observed_at"])
        self.assertEqual(cached["events"], initial["events"])

    async def test_selected_message_replay_exceeds_live_window_and_excludes_other_message(self):
        for _ in range(650):
            self.api.event('span.model_request_start',message_id=self.message['id'])
        newer=self.api.event('user.message',content=[{'type':'text','text':'另一条当前任务'}])
        for _ in range(1250):
            self.api.event('span.model_request_start',message_id=newer['id'])
        live=await (await self.client.get('/api/observer')).json()
        self.assertIsNone(live['message_id'])
        self.assertNotIn(self.message['id'],[turn['id'] for turn in live['session']['turns']])
        response=await self.client.get('/api/observer',params={'message_id':self.message['id']})
        self.assertEqual(response.status,200)
        replay=await response.json()
        self.assertEqual(replay['message_id'],self.message['id'])
        self.assertEqual([turn['id'] for turn in replay['session']['turns']],[self.message['id']])
        self.assertGreater(len(replay['events']),600)
        self.assertTrue(all(event['message_id']==self.message['id'] for event in replay['events']))
        self.assertTrue(all(event.get('message_id')==self.message['id'] for event in replay['execution']['events']))
        self.assertFalse(replay['history_incomplete'])
        self.assertEqual(replay['worker']['state'],'healthy')
        self.assertEqual(self.api.requests,[])

    async def test_replay_tool_order_uses_real_request_start_finish_result_times(self):
        for event in self.api.feed:
            if event['id']==self.message['id']:
                event['created_at']=self.now-10
            elif event['id']==self.call['id']:
                event['created_at']=self.now-9
            elif event['type']=='user.tool_result':
                event['created_at']=self.now-7
        records=[json.loads(line) for line in self.log.read_text().splitlines()]
        for event in records:
            if event['kind']=='tool_start':
                event['time']=self.now-8
            elif event['kind']=='tool_finished':
                event['time']=self.now-7.993
            elif event['kind']=='tool_posted':
                event['time']=self.now-7.5
        self.log.write_text(''.join(json.dumps(event)+'\n' for event in records))
        data=await (await self.client.get('/api/observer',params={'message_id':self.message['id']})).json()
        calls=[event for event in data['events'] if event['tool_id']==self.call['id']]
        self.assertEqual([event['kind'] for event in calls],['agent.tool_use','tool_started','tool_execution','user.tool_result'])
        self.assertEqual(calls[1]['time'],self.now-8)
        self.assertEqual(calls[2]['time'],self.now-7.993)
        self.assertEqual(calls[2]['elapsed_ms'],7)
        self.assertNotIn('elapsed_ms',calls[1])

    async def test_history_cache_is_scoped_by_message_and_invalid_or_unknown_ids_do_not_send(self):
        newer=self.api.event('user.message',content=[{'type':'text','text':'第二条任务'}])
        read=self.live.snapshot
        requested=[]
        def counted(**kwargs):
            requested.append(kwargs.get('message_id'))
            return read(**kwargs)
        self.live.snapshot=counted
        default=await (await self.client.get('/api/observer')).json()
        first=await (await self.client.get('/api/observer',params={'message_id':self.message['id']})).json()
        repeated=await (await self.client.get('/api/observer',params={'message_id':self.message['id']})).json()
        second=await (await self.client.get('/api/observer',params={'message_id':newer['id']})).json()
        self.assertIsNone(default['message_id'])
        self.assertEqual(first,repeated)
        self.assertEqual(second['message_id'],newer['id'])
        self.assertEqual(requested,[None,self.message['id'],newer['id']])
        for params,status in [({'message_id':''},400),({'message_id':'../private-upstream-token'},400),
                              ({'message_id':'unobserved-message'},404),({'message_id':self.call['id']},404),
                              ([('message_id',self.message['id']),('message_id',newer['id'])],400)]:
            response=await self.client.get('/api/observer',params=params)
            self.assertEqual(response.status,status)
            self.assertNotIn('private-upstream-token',await response.text())
        response=await self.client.post('/api/observer',params={'message_id':self.message['id']},json={'text':'never-send'})
        self.assertEqual(response.status,405)
        self.assertEqual(self.api.requests,[])

    async def test_replay_redacts_credentials_and_reports_missing_local_history(self):
        self.api.event('agent.message',message_id=self.message['id'],content=[
            {'type':'text','text':'Bearer private-upstream-token'},
            {'type':'thinking','text':'hidden-private-reasoning'}])
        # The actual log cap has discarded earlier local execution, while the
        # QCA public tool request/result remain in the persistent journal.
        with self.log.open('a') as stream:
            stream.write('x'*(2*1024*1024+100)+'\n')
        data=await (await self.client.get('/api/observer',params={'message_id':self.message['id']})).json()
        self.assertTrue(data['history_incomplete'])
        self.assertFalse(any(event['kind'] in {'tool_started','tool_execution'} for event in data['events']))
        self.assertTrue(any(event['kind']=='agent.tool_use' for event in data['events']))
        for private in ('private-upstream-token','private-work-secret','private-metadata','hidden-private-reasoning'):
            self.assertNotIn(private,json.dumps(data))
        self.assertEqual(self.api.requests,[])

    async def offline_client(self):
        app=await create_app({'analysis_url':'http://127.0.0.1:1','analysis_token':'private-upstream-token'},
                             self.status_provider,self.live)
        client=TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_public_execution_archive_survives_preview_restart_without_refreshing_time_or_health(self):
        fresh=await (await self.client.get('/api/execution')).json()
        archive=self.root/'execution-history.json'
        self.assertEqual(archive.stat().st_mode & 0o777,0o600)
        saved=json.loads(archive.read_bytes())
        self.assertEqual(saved['session_id'],'sess-a')
        self.assertEqual(saved['run_id'],'run-test')
        self.assertEqual(saved['execution']['observed_at'],fresh['observed_at'])
        self.log.unlink()
        restarted=await self.offline_client()
        offline=await (await restarted.get('/api/observer',params={'message_id':self.message['id']})).json()
        self.assertTrue(offline['execution']['stale'])
        self.assertEqual(offline['execution']['observed_at'],fresh['observed_at'])
        self.assertEqual(offline['execution']['history_source'],'archive')
        self.assertEqual(offline['worker']['state'],'unknown')
        self.assertTrue(any('Use existing' in event.get('output','') for event in offline['events']))
        for secret in ('private-upstream-token','private-work-secret','private-metadata'):
            self.assertNotIn(secret,archive.read_text())

    async def test_cold_start_projects_existing_local_worker_log_with_visible_python_io(self):
        self.record('tool_start',tool='run_python',tool_use_id='python-call',message_id=self.message['id'],
                    input={'code':'print("offline-visible")'})
        self.record('tool_finished',tool='run_python',tool_use_id='python-call',message_id=self.message['id'],
                    output='exit_code=0\noffline-visible',elapsed_ms=3)
        self.record('tool_start',tool='Bash',tool_use_id='generic',message_id=self.message['id'],input={'command':'private-command'})
        self.record('tool_finished',tool='Bash',tool_use_id='generic',message_id=self.message['id'],output='private-generic-output')
        (self.root/'identity.json').unlink()
        restarted=await self.offline_client()
        replay=await (await restarted.get('/api/observer',params={'message_id':self.message['id']})).json()
        self.assertTrue(replay['execution']['stale'])
        self.assertIsNone(replay['execution']['observed_at'])
        self.assertEqual(replay['execution']['history_source'],'worker_log')
        self.assertNotEqual(replay['worker']['state'],'healthy')
        python=next(event for event in replay['events'] if event['kind']=='tool_execution' and event['tool_id']=='python-call')
        self.assertEqual(python['code'],'print("offline-visible")')
        self.assertEqual(python['output'],'exit_code=0\noffline-visible')
        self.assertTrue((self.root/'execution-history.json').is_file())
        for secret in ('private-command','private-generic-output','private-upstream-token'):
            self.assertNotIn(secret,json.dumps(replay))

    async def test_foreign_or_oversized_archive_and_other_session_log_are_never_loaded(self):
        archive=self.root/'execution-history.json'
        archive.write_text(json.dumps({'version':2,'session_id':'foreign','run_id':'run-test',
                                       'execution':{'events':[{'output':'foreign-private-output'}]}}))
        self.log.write_text(json.dumps({'session_id':'foreign','kind':'tool_start','tool':'run_python',
                                       'tool_use_id':'foreign','input':{'code':'foreign-private-code'}})+'\n')
        first=await self.offline_client()
        data=await (await first.get('/api/execution')).json()
        self.assertEqual(data['events'],[])
        self.assertNotIn('foreign-private',json.dumps(data))
        archive.write_text('x'*(8*1024*1024+1))
        second=await self.offline_client()
        data=await (await second.get('/api/execution')).json()
        self.assertEqual(data['events'],[])
        self.assertTrue(data['stale'])

    async def test_archive_size_limit_marks_dropped_history_and_leaves_no_temporary_file(self):
        for number in range(10):
            self.record('tool_start',tool='run_python',tool_use_id='large-'+str(number),message_id=self.message['id'],
                        input={'code':'# '+('public-code '*100)+'\nprint(1)'})
            self.record('tool_finished',tool='run_python',tool_use_id='large-'+str(number),message_id=self.message['id'],output='exit_code=0\n1')
        with patch('qoder_byoc_sandbox_example.demos.report_analyst.preview.MAX_HISTORY_BYTES',4096):
            fresh=await (await self.client.get('/api/execution')).json()
            archive=self.root/'execution-history.json'
            self.assertLessEqual(archive.stat().st_size,4096)
            saved=json.loads(archive.read_bytes())
        self.assertTrue(saved['execution']['truncated'])
        self.assertLess(len(saved['execution']['events']),len(fresh['events']))
        self.assertEqual(list(self.root.glob('.execution-history-*')),[])

    async def test_replay_missing_tool_io_is_incomplete_even_when_saved_log_was_not_truncated(self):
        message=self.api.event('user.message',content=[{'type':'text','text':'后续自由对话'}])
        call=self.api.event('agent.custom_tool_use',name='run_python',input={'code':'not-saved-locally'})
        self.api.event('user.custom_tool_result',custom_tool_use_id=call['id'])
        self.api.event('session.status_idle',stop_reason={'type':'end_turn'})
        replay=await (await self.client.get('/api/observer',params={'message_id':message['id']})).json()
        self.assertTrue(replay['history_incomplete'])
        self.assertTrue(replay['execution']['history_incomplete'])
        self.assertFalse(replay['execution']['truncated'])
        self.assertEqual(replay['execution']['events'],[])
        result=next(event for event in replay['events'] if event['kind']=='user.custom_tool_result')
        self.assertEqual(result['output'],'')
        self.assertFalse(any(event['kind'] in {'tool_started','tool_execution'} for event in replay['events']))
        denied_message=self.api.event('user.message',content=[{'type':'text','text':'未授权请求'}])
        self.api.event('agent.tool_use',name='Bash',evaluated_permission='deny',input={'command':'never-executed'})
        self.api.event('session.status_idle',stop_reason={'type':'end_turn'})
        denied=await (await self.client.get('/api/observer',params={'message_id':denied_message['id']})).json()
        self.assertFalse(denied['history_incomplete'])
        self.assertEqual(denied['execution']['events'],[])
        self.assertEqual(self.api.requests,[])

    async def test_legacy_raw_import_archive_cannot_bypass_safe_read_projection(self):
        marker='unverified historical document contents'
        records=[json.loads(line) for line in self.log.read_text().splitlines()]
        for event in records:
            if event['kind']=='tool_finished' and event.get('tool')=='Read':
                event['output']=marker
        self.log.write_text(''.join(json.dumps(event)+'\n' for event in records))
        legacy=execution_snapshot(self.root,self.log)
        self.assertIn(marker,json.dumps(legacy))
        archive=self.root/'execution-history.json'
        archive.write_text(json.dumps({'version':1,'session_id':'sess-a','run_id':'run-test',
                                       'execution':dict(legacy,observed_at=123)}))
        restarted=await self.offline_client()
        data=await (await restarted.get('/api/observer',params={'message_id':self.message['id']})).json()
        self.assertEqual(data['version'],1)
        self.assertEqual(data['execution']['history_source'],'worker_log')
        self.assertTrue(data['execution']['stale'])
        self.assertNotIn(marker,json.dumps(data))
        self.assertNotIn(marker,archive.read_text())
        self.assertEqual(json.loads(archive.read_bytes())['version'],2)

    async def test_run_scoped_api_journals_merge_revalidate_and_survive_archive_restart(self):
        from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder
        local=APITraceRecorder(self.root/'api-trace.jsonl','run-test',session_id='sess-a',secrets=('private-upstream-token',))
        def record(recorder, identifier, path, body, response):
            recorder.record(dict(id=identifier,method='POST',path=path,state='success',status_code=201,attempt=1,
                started_at=self.now,finished_at=self.now+0.1,elapsed_ms=12,
                request={'headers':{'Authorization':'Bearer private-upstream-token','Content-Type':'application/json'},'body':body},
                response={'headers':{'X-Request-ID':identifier,'Set-Cookie':'private-cookie'},'body':response}))
        record(local,'http-local','/agents',{'name':'Demo'},{'id':'agent-a','type':'agent'})
        remote=APITraceRecorder(self.root/'remote-api.jsonl','run-test',session_id='sess-a')
        record(remote,'http-worker','/sessions/sess-a/events',
               {'events':[{'type':'user.tool_result','tool_use_id':self.call['id']}]},
               {'data':[{'id':'result-api','type':'user.tool_result','tool_use_id':self.call['id']}]})
        record(remote,'http-old-work','/environments/env-a/work/old-work/heartbeat',{},
               {'id':'old-work','data':{'id':'sess-a','type':'session'},'ttl_seconds':60})
        foreign=APITraceRecorder(self.root/'foreign-api.jsonl','other-run',session_id='sess-a')
        record(foreign,'http-foreign','/sessions/sess-a/events',{}, {'data':[]})
        imported=remote.snapshot()
        imported['requests'].extend(foreign.snapshot()['requests'])
        # A remote public envelope is still untrusted; do not echo arbitrary
        # headers/fields just because it originally claims to be projected.
        imported['requests'][0]['request']['headers']['authorization']='private-untrusted-auth'
        imported['requests'][0]['response']['body']['private']='private-remote-field'
        original=execution_snapshot(self.root,self.log)
        with patch('qoder_byoc_sandbox_example.demos.report_analyst.execution.execution_snapshot',
                   return_value=dict(original,api_trace=imported)):
            data=await (await self.client.get('/api/observer')).json()
        ids={r['id'] for r in data['api_trace']['requests']}
        self.assertEqual(ids,{'http-local','http-worker','http-old-work'})
        self.assertTrue(data['api_trace']['history_incomplete'])
        self.assertEqual(data['api_trace']['sources'],{'http-local':'control','http-worker':'worker','http-old-work':'worker'})
        trace_rows=[r for g in data['tracing']['groups'] for r in g['rows']]
        posted=next(r for r in trace_rows if r['id']=='http:http-worker')
        self.assertEqual(posted['source'],'worker_qca_http')
        self.assertEqual(posted['role'],'worker')
        self.assertEqual(posted['parent_id'],'tool:'+self.call['id'])
        self.assertEqual(posted['message_id'],self.message['id'])
        self.assertTrue(any(g['scope']=='background' and any(r['id']=='http:http-old-work' for r in g['rows'])
                            for g in data['tracing']['groups']))
        for secret in ('private-upstream-token','private-cookie','private-untrusted-auth','private-remote-field'):
            self.assertNotIn(secret,json.dumps(data))
        saved=json.loads((self.root/'execution-history.json').read_text())
        self.assertEqual({r['id'] for r in saved['execution']['api_trace']['requests']},{'http-worker','http-old-work'})
        limited=await self.offline_client()
        with patch('qoder_byoc_sandbox_example.demos.report_analyst.preview.MAX_ENTRIES',2):
            bounded=await (await limited.get('/api/observer')).json()
        self.assertEqual(len(bounded['api_trace']['requests']),2)
        self.assertIn('http-local',{r['id'] for r in bounded['api_trace']['requests']})
        self.assertTrue(bounded['api_trace']['history_incomplete'])
        (self.root/'api-trace.jsonl').unlink()
        self.log.unlink()
        restarted=await self.offline_client()
        old=await (await restarted.get('/api/observer')).json()
        self.assertEqual({r['id'] for r in old['api_trace']['requests']},{'http-worker','http-old-work'})
        self.assertTrue(old['execution']['stale'])
        self.assertEqual(self.api.requests,[])
    async def test_single_turn_replay_never_reimports_later_input_api_bodies(self):
        from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder
        later=self.api.event('user.message',content=[{'type':'text','text':'future-input-body-marker'}])
        later_call=self.api.event('agent.custom_tool_use',name='run_python',input={'code':'print("future-code-marker")'})
        self.api.event('user.custom_tool_result',custom_tool_use_id=later_call['id'])
        self.api.event('agent.message',content=[{'type':'text','text':'future-reply-marker'}])
        self.api.event('session.status_idle',stop_reason={'type':'end_turn'})
        self.record('tool_start',tool='run_python',tool_use_id=later_call['id'],message_id=later['id'],input={'code':'print("future-code-marker")'})
        self.record('tool_finished',tool='run_python',tool_use_id=later_call['id'],message_id=later['id'],output='exit_code=0\nfuture-output-marker',elapsed_ms=8)
        recorder=APITraceRecorder(self.root/'api-trace.jsonl','run-test',session_id='sess-a')
        def captured(identifier, body, response, method='POST', path='/sessions/sess-a/events'):
            recorder.record(dict(id=identifier,method=method,path=path,state='success',status_code=201 if method=='POST' else 200,
                attempt=1,started_at=self.now,finished_at=self.now+0.1,elapsed_ms=10,
                request={'body':body},response={'body':response}))
        captured('input-a',{'events':[{'type':'user.message','content':[{'type':'text','text':'first-input-body-marker'}]}]},
                 {'data':[{'id':self.message['id'],'type':'user.message'}]})
        captured('tool-a',{'events':[{'type':'user.tool_result','tool_use_id':self.call['id']}]},
                 {'data':[{'id':'posted-a','type':'user.tool_result','tool_use_id':self.call['id']}]})
        captured('input-b',{'events':[{'type':'user.message','content':[{'type':'text','text':'future-input-body-marker'}]}]},
                 {'data':[{'id':later['id'],'type':'user.message'}]})
        captured('tool-b',{'events':[{'type':'user.custom_tool_result','custom_tool_use_id':later_call['id'],
                                    'content':[{'type':'text','text':'future-api-output-marker'}]}]},
                 {'data':[{'id':'posted-b','type':'user.custom_tool_result','custom_tool_use_id':later_call['id']}]})
        captured('poll',None,{'data':[{'id':later['id'],'type':'user.message','content':[{'type':'text','text':'future-poll-body-marker'}]}]},method='GET')
        first=await (await self.client.get('/api/observer',params={'message_id':self.message['id']})).json()
        expected={'input-a','tool-a'}
        self.assertEqual({r['id'] for r in first['api_trace']['requests']},expected)
        self.assertEqual({r['id'] for r in first['execution']['api_trace']['requests']},expected)
        self.assertEqual({e['api_request_id'] for e in first['events'] if e['kind']=='http.request'},expected)
        self.assertNotIn('future-',json.dumps(first))
        second=await (await self.client.get('/api/observer',params={'message_id':later['id']})).json()
        self.assertEqual({r['id'] for r in second['api_trace']['requests']},{'input-b','tool-b'})
        self.assertNotIn('first-input-body-marker',json.dumps(second))
        live=await (await self.client.get('/api/observer')).json()
        self.assertEqual(len(live['api_trace']['requests']),5)
        self.assertIn('future-input-body-marker',json.dumps(live))
        self.assertEqual(self.api.requests,[])

    async def test_live_and_replay_share_component_handoffs_not_internal_records(self):
        for index in range(50):
            self.record('heartbeat_confirmed',work_id='work-a',ttl_seconds=60)
            self.api.event('span.model_request_start',message_id=self.message['id'])
        live=await (await self.client.get('/api/observer')).json()
        replay=await (await self.client.get('/api/observer',params={'message_id':self.message['id']})).json()
        a=next(item for item in live['interactions']['conversations'] if item['message_id']==self.message['id'])
        b=next(item for item in replay['interactions']['conversations'] if item['message_id']==self.message['id'])
        self.assertEqual(a['number'],1)
        self.assertEqual(b['number'],1)
        self.assertEqual(a['total_steps'],5)
        self.assertEqual(b['total_steps'],5)
        self.assertEqual([(s['id'],s['number']) for s in a['steps']],[(s['id'],s['number']) for s in b['steps']])
        self.assertEqual(live['interactions']['version'],4)
        raw={event['id']:event for event in live['events']}
        kinds=[step['source_kind'] for step in a['steps']]
        self.assertEqual(kinds,['user.message','agent.tool_use','tool_started','user.tool_result','agent.message'])
        self.assertEqual([(s['source'],s['target']) for s in a['steps']],
                         [('input','session'),('loop','events'),('events','worker'),('worker','events'),('loop','session')])
        self.assertEqual(len({step['number'] for step in a['steps']}),5)
        self.assertFalse(any(raw[key]['category'] in {'heartbeat','work','session','http'} for step in a['steps'] for key in step['event_ids']))
        self.assertNotIn('_flow_events',live)
        self.assertEqual(self.api.requests,[])

    async def test_python_markers_keep_live_and_replay_component_endpoints_identical(self):
        message = self.api.event('user.message', content=[dict(type='text', text='执行 Python')])
        call = self.api.event('agent.custom_tool_use', name='run_python')
        self.record('tool_start', tool='run_python', tool_use_id=call['id'], message_id=message['id'],
                    input={'code':'print(42)'}, work_id='work-a')
        self.record('tool_stage', stage='python_dispatch', tool_use_id=call['id'], role='analysis')
        live = await (await self.client.get('/api/observer')).json()
        first = next(c for c in live['interactions']['conversations'] if c['message_id'] == message['id'])
        self.assertEqual([(s['source'],s['target']) for s in first['steps']],
                         [('input','session'),('loop','events'),('events','worker'),('worker','python')])
        self.record('tool_stage', stage='python_return', tool_use_id=call['id'], role='analysis')
        self.record('tool_finished', tool='run_python', tool_use_id=call['id'], message_id=message['id'],
                    output='exit_code=0\n42', elapsed_ms=4, work_id='work-a')
        self.record('tool_posted', tool='run_python', tool_use_id=call['id'], work_id='work-a')
        self.api.event('user.custom_tool_result', custom_tool_use_id=call['id'])
        self.api.event('agent.message', content=[dict(type='text',text='完成')])
        self.api.event('session.status_idle', stop_reason={'type':'end_turn'})
        # Endpoint snapshots are intentionally cached for one second.
        await asyncio.sleep(1.05)
        live = await (await self.client.get('/api/observer')).json()
        replay = await (await self.client.get('/api/observer',params={'message_id':message['id']})).json()
        a = next(c for c in live['interactions']['conversations'] if c['message_id'] == message['id'])
        b = replay['interactions']['conversations'][0]
        self.assertEqual(a['total_steps'],7)
        self.assertEqual([(s['id'],s['number'],s['source'],s['target'],s['flow']) for s in a['steps']],
                         [(s['id'],s['number'],s['source'],s['target'],s['flow']) for s in b['steps']])
        self.assertEqual({s['id']:s['number'] for s in first['steps']},
                         {s['id']:s['number'] for s in a['steps'] if s['number'] <= 4})
        self.assertEqual(self.api.requests,[])

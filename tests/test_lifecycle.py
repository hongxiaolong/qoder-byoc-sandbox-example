"""Resource history distinguishes creation facts from observation/reuse checks."""
import json
from pathlib import Path
import tempfile
import unittest

from qoder_byoc_sandbox_example.demos.report_analyst.demo_status import DemoStatus
from qoder_byoc_sandbox_example.demos.report_analyst.lifecycle import record_created, public_history


class ResourceHistoryTests(unittest.TestCase):
    def test_creation_is_recorded_once_without_raw_sdk_fields(self):
        state = {}
        response = {"created_at": "2026-10-31T00:00:00Z", "secret": "never-record"}
        record_created(state, "agent", "agent-a", response)
        record_created(state, "agent", "agent-a", response)
        self.assertEqual(len(state["lifecycle"]), 1)
        self.assertNotIn("never-record", json.dumps(state))
        state["lifecycle"][0]["raw"] = "never-expose"
        self.assertNotIn("never-expose", json.dumps(public_history(state["lifecycle"])))

    def test_existing_resource_checks_are_real_ordered_and_not_repeated_each_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"run.json"
            state = dict(run_id="run-a", agent_id="agent-a", environment_id="env-a", session_id="sess-a",
                         analysis_url="https://analysis.invalid", worker_pid=10)
            path.write_text(json.dumps(state))
            class Client:
                calls=[]
                def request(self, method, route):
                    self.calls.append((method,route))
                    value = {"id":route.rsplit("/",1)[-1], "created_at":"2026-10-31T00:00:00Z",
                             "metadata":{"demo_run":"run-a"},"environment_id":"env-a","agent":{"id":"agent-a"}}
                    return value
            client=Client()
            status=DemoStatus(path,state["analysis_url"],client,{},3600,clock=lambda:2000000000)
            first=status.snapshot()["lifecycle"]
            self.assertEqual([e['kind'] for e in first], ["resource_created","resource_checked","resource_reused"]*3)
            self.assertTrue(all(e['source']=="qca_record" for e in first if e['kind']=="resource_created"))
            self.assertEqual(status.snapshot()["lifecycle"], first)
            self.assertEqual(len(client.calls),3)

    def test_unknown_creation_time_and_wrong_binding_do_not_invent_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"run.json"
            path.write_text(json.dumps(dict(run_id="r",agent_id="agent-a",environment_id="env-a",session_id="sess-a",
                                            analysis_url="a",worker_pid=1)))
            class Client:
                def request(self, method, route):
                    return dict(id=route.rsplit('/',1)[-1],created_at="invalid",metadata={"demo_run":"r"},
                                agent={"id":"other-agent"},environment_id="env-a")
            data=DemoStatus(path,"a",Client(),{},3600).snapshot()["lifecycle"]
            self.assertFalse(any(e['kind']=="resource_created" for e in data))
            self.assertTrue(any(e['resource']=="agent" and e['kind']=="resource_check_failed" for e in data))
            self.assertFalse(any(e['resource']=="agent" and e['kind']=="resource_reused" for e in data))

    def test_cleaned_run_reads_creation_facts_without_claiming_active_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"run.json"
            path.write_text(json.dumps(dict(run_id="r",cleaned=True,agent_id="agent-a",environment_id="env-a",session_id="sess-a")))
            class Client:
                def request(self, method, route):
                    return dict(id=route.rsplit('/',1)[-1],created_at="2026-10-31T00:00:00Z",
                                archived_at="2026-10-31T01:00:00Z",metadata={"demo_run":"r"})
            data=DemoStatus(path,"a",Client(),{},3600).snapshot()
            self.assertEqual(data['state'],'expired')
            self.assertEqual([e['kind'] for e in data['lifecycle']],["resource_created"]*3)

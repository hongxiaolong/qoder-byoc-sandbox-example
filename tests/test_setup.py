# SPDX-License-Identifier: Apache-2.0
"""验证部分创建后的恢复不会覆盖已记录资源，也不会进入再次创建流程。"""

import unittest

from qoder_byoc_sandbox_example.demos.report_analyst.cli import finish_setup


class SetupRecoveryTests(unittest.TestCase):
    def test_existing_qoder_resource_blocks_recreation_before_runtime_access(self):
        for key in ("skill_id", "agent_id", "environment_id", "session_id"):
            with self.subTest(resource=key):
                state = {"run_id": "setup-recovery-test", key: "existing-resource"}
                before = state.copy()
                # 无配置、连接或凭证：必须先拒绝旧状态，不能进入实际创建流程。
                with self.assertRaisesRegex(ValueError, "cleanup.*setup"):
                    finish_setup(None, None, state, None, None)
                self.assertEqual(state, before)

    def test_multiple_recorded_resources_are_preserved(self):
        state = {
            "run_id": "setup-recovery-test",
            "skill_id": "existing-skill",
            "agent_id": "existing-agent",
            "environment_id": "existing-environment",
        }
        before = state.copy()
        with self.assertRaisesRegex(ValueError, "cleanup.*setup"):
            finish_setup(None, None, state, None, None)
        self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()

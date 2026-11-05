# SPDX-License-Identifier: Apache-2.0
"""演示目标：验证通用 Worker 与报表分析示例的依赖关系和命令入口。
执行流程：禁止加载演示模块与可选依赖后执行真实文件工具；使用独立配置目录与子进程。
能力体现：通用执行端可独立使用，演示通过工具提供器接入，重命名后入口仍能启动。"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class IndependenceTests(unittest.TestCase):
    def test_core_worker_without_demo_or_sandbox_dependencies(self):
        source = r"""
import importlib.abc
import sys
import tempfile
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('qoder_byoc_sandbox_example.demos', 'e2b', 'playwright', 'pandas',
                   'aiohttp', 'plotly', 'openpyxl', 'alibabacloud_sandbox20260820')
        if any(fullname == p or fullname.startswith(p + '.') for p in blocked):
            raise ImportError('Optional dependency requested: ' + fullname)
sys.meta_path.insert(0, BlockOptional())
from qoder_byoc_sandbox_example.environments.self_hosted.worker import Worker
with tempfile.TemporaryDirectory() as root:
    worker = Worker(None, 'env', 'session', 'worker', root, root + '/state')
    text, error = worker.tools.execute('Write', {'file_path':'answer.txt', 'content':'42'})
    assert not error, text
    text, error = worker.tools.execute('Read', {'file_path':'answer.txt'})
    assert not error and text == '1: 42', (text, error)
print('core_worker_independent')
"""
        result = subprocess.run(
            [sys.executable, "-c", source],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core_worker_independent", result.stdout)

    def test_command_modules_can_start(self):
        for module in [
            "qoder_byoc_sandbox_example.environments.self_hosted.worker",
            "qoder_byoc_sandbox_example.demos.report_analyst.cli",
            "qoder_byoc_sandbox_example.sandbox.templates",
        ]:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_configuration_works_outside_demo_directory(self):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config"
            config.mkdir()
            (config / "app.toml").write_text(
                '[qoder]\nbase_url="https://example.invalid"\n'
            )
            (config / ".env").write_text(
                "E2B_API_KEY=local-test-key\nE2B_API_URL=https://example.invalid\nE2B_DOMAIN=example.invalid\n"
            )
            env = {**os.environ, "QODER_SANDBOX_HOME": root}
            source = """from qoder_byoc_sandbox_example.settings import load
config, values = load(required=['E2B_API_KEY','E2B_API_URL','E2B_DOMAIN'])
assert values['E2B_API_KEY'] == 'local-test-key'
assert 'agent' not in config and 'demo' not in config
"""
            result = subprocess.run(
                [sys.executable, "-c", source], env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)

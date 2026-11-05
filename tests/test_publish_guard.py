import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_guard", ROOT / "scripts/check_publish.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicationGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        (self.root / ".gitignore").write_text((ROOT / ".gitignore").read_text())
        (self.root / "config").mkdir()
        self.value = uuid.uuid4().hex
        (self.root / "config/.env").write_text("QODER_TOKEN=" + self.value + "\n")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE)

    def test_local_credentials_and_backup_files_are_excluded(self):
        for name in ["config/.env.bak", ".env.cloud.old", "config/registry.toml.bak", "private.key"]:
            (self.root / name).write_text(self.value)
            self.git("check-ignore", "--quiet", name)
        (self.root / "config/.env.example").write_text("QODER_TOKEN=replace-me\n")
        self.git("add", ".gitignore", "config/.env.example")
        findings, _ = guard.audit(self.root)
        self.assertEqual(findings, [])

    def test_index_is_checked_even_after_working_file_is_sanitized(self):
        target = self.root / "notes.txt"
        target.write_text(self.value)
        self.git("add", "notes.txt")
        target.write_text("sanitized\n")
        findings, _ = guard.audit(self.root)
        self.assertTrue(any(scope == "index" and name == "notes.txt" for scope, name, _, _ in findings))
        self.assertNotIn(self.value, repr(findings))

    def test_removed_credentials_remain_detectable_in_history(self):
        target = self.root / "notes.txt"
        target.write_text(self.value)
        self.git("add", "notes.txt")
        self.git("-c", "user.name=Security Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        target.write_text("sanitized\n")
        self.git("add", "notes.txt")
        self.git("-c", "user.name=Security Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove fixture")
        self.assertEqual(guard.audit(self.root)[0], [])
        findings, counts = guard.audit(self.root, history=True)
        self.assertTrue(any(scope == "history" for scope, _, _, _ in findings))
        self.assertGreater(counts["history_blobs"], 0)
        self.assertNotIn(self.value, repr(findings))

    def test_force_added_private_config_is_blocked(self):
        self.git("add", "-f", "config/.env")
        findings, _ = guard.audit(self.root)
        self.assertTrue(any(kind == "private/generated path" for _, _, _, kind in findings))
        self.assertNotIn(self.value, repr(findings))

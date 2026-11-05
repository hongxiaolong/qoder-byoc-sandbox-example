"""Completed demo milestones remain separate from later interactive edits."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from onboarding import completion_baseline


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        for phase, rows in [("collect", 100), ("revise", 65), ("append", 101)]:
            self.state[phase + "_verified"] = True
            self.state[phase + "_snapshot"] = {"row_count": rows, "revision": len(self.state) // 2 + 1}

    def test_returns_verified_milestones_without_mutating_them(self):
        before = deepcopy(self.state)
        baseline = completion_baseline(self.state)
        self.assertEqual(baseline["append"]["row_count"], 101)
        self.assertEqual(self.state, before)

    def test_missing_or_failed_milestones_cannot_skip_verification(self):
        for phase in ["collect", "revise", "append"]:
            for key, value in [(phase + "_verified", False), (phase + "_snapshot", {})]:
                with self.subTest(key=key):
                    invalid = {**self.state, key: value}
                    with self.assertRaises(ValueError):
                        completion_baseline(invalid)

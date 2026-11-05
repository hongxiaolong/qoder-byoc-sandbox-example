# SPDX-License-Identifier: Apache-2.0
"""独立验收必须发现总额不变的原记录损坏和错误的追加内容。"""

from copy import deepcopy
import unittest

from qoder_byoc_sandbox_example.demos.report_analyst.data import (
    aggregate,
    read_rows,
    seed_csv,
    selected,
)
from qoder_byoc_sandbox_example.demos.report_analyst.verification import (
    input_integrity_checks,
)


class InputIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.original = selected(read_rows(seed_csv()), "2026-10", "华东")
        self.added = {
            "date": "2026-10-29",
            "category": "现场演示",
            "region": "华东",
            "amount": "6666.00",
        }

    def test_collect_rejects_changed_records_even_with_same_total(self):
        corrupted = deepcopy(self.original)
        corrupted[0]["region"] = "华南"
        self.assertEqual(
            aggregate(corrupted)["total_amount"],
            aggregate(self.original)["total_amount"],
        )
        checks = input_integrity_checks("collect", corrupted, self.original)
        self.assertFalse(all(checks.values()))

    def test_append_rejects_corrupted_original_rows(self):
        corrupted = deepcopy(self.original)
        corrupted[0]["date"] = (
            "2026-10-01" if corrupted[0]["date"] != "2026-10-01" else "2026-10-02"
        )
        corrupted.append(self.added)
        self.assertEqual(len(corrupted), len(self.original) + 1)
        self.assertAlmostEqual(
            aggregate(corrupted)["total_amount"]
            - aggregate(self.original)["total_amount"],
            6666,
        )
        self.assertFalse(
            all(input_integrity_checks("append", corrupted, self.original).values())
        )

    def test_append_rejects_wrong_date_region_category_or_amount(self):
        for field, value in [
            ("date", "2026-10-28"),
            ("region", "华北"),
            ("category", "其他"),
            ("amount", "6667.00"),
        ]:
            with self.subTest(field=field):
                record = {**self.added, field: value}
                self.assertFalse(
                    all(input_integrity_checks(
                        "append", self.original + [record], self.original
                    ).values())
                )

    def test_unmodified_download_and_exact_append_are_accepted(self):
        self.assertTrue(
            all(input_integrity_checks("collect", self.original, self.original).values())
        )
        self.assertTrue(
            all(input_integrity_checks(
                "append", self.original + [self.added], self.original
            ).values())
        )

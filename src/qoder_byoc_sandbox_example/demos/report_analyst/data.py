# SPDX-License-Identifier: Apache-2.0
"""演示数据与独立核算。

模块定位：生成明确标注的合成数据，提供与 Agent 分析代码独立的 Decimal 核算。
主要逻辑：按月份、地区筛选 CSV，校验汇总、分类、地区和日期四种口径。
支撑能力：现场追加记录后核对行数及金额变化，不用模型答复代替实际结果。
"""

import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import random

FIELDS = ["date", "category", "region", "amount"]


def seed_csv():
    rng = random.Random(20261029)
    rows = []
    for i in range(600):
        day = date(2026, 9, 1) + timedelta(days=rng.randrange(59))
        rows.append(
            dict(
                date=day.isoformat(),
                category=rng.choice(["计算", "存储", "网络", "数据库"]),
                region=rng.choice(["华东", "华南", "华北"]),
                amount=f"{rng.randrange(1000, 90000) / 100:.2f}",
            )
        )
    return csv_bytes(rows)


def csv_bytes(rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def read_rows(data):
    if isinstance(data, Path):
        data = data.read_bytes()
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    for row in rows:
        if set(row) != set(FIELDS):
            raise ValueError("CSV schema must be date/category/region/amount")
        date.fromisoformat(row["date"])
        value = Decimal(row["amount"])
        if not value.is_finite():
            raise ValueError("Amount must be finite")
    return rows


def selected(rows, month="", region="", excluded=()):
    return [
        r
        for r in rows
        if (not month or r["date"].startswith(month))
        and (not region or region == "全部" or r["region"] == region)
        and r["category"] not in excluded
    ]


def aggregate(rows, excluded=()):
    rows = selected(rows, excluded=excluded)
    result = {
        "row_count": len(rows),
        "total_amount": 0,
        "category_totals": {},
        "region_totals": {},
        "daily_totals": {},
    }
    total = Decimal(0)
    for row in rows:
        value = Decimal(row["amount"])
        total += value
        for field, key in [
            ("category", "category_totals"),
            ("region", "region_totals"),
            ("date", "daily_totals"),
        ]:
            group = result[key]
            group[row[field]] = group.get(row[field], Decimal(0)) + value
    result["total_amount"] = float(total)
    for key in ["category_totals", "region_totals", "daily_totals"]:
        result[key] = {k: float(v) for k, v in sorted(result[key].items())}
    return result


def check_analysis(result, rows):
    excluded = result.get("filters", {}).get("exclude_categories", [])
    if not isinstance(excluded, list) or not all(isinstance(x, str) for x in excluded):
        raise ValueError("filters.exclude_categories must be a list of strings")
    expected = aggregate(rows, excluded)
    if result.get("row_count") != expected["row_count"]:
        raise ValueError("Agent row_count differs from independent CSV count")
    for key in ["total_amount", "category_totals", "region_totals", "daily_totals"]:
        actual = result.get(key)
        target = expected[key]
        if isinstance(target, dict):
            if not isinstance(actual, dict) or set(actual) != set(target):
                raise ValueError(f"Agent {key} has different group keys")
            pairs = ((actual[k], v) for k, v in target.items())
        else:
            pairs = [(actual, target)]
        for a, b in pairs:
            try:
                amount = Decimal(str(a))
                if not amount.is_finite() or abs(amount - Decimal(str(b))) > Decimal(
                    "0.005"
                ):
                    raise ValueError()
            except Exception:
                raise ValueError(
                    f"Agent {key} differs from independent Decimal calculation"
                ) from None
    return expected


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)

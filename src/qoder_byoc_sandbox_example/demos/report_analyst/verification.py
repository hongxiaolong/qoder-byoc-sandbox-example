# SPDX-License-Identifier: Apache-2.0
"""演示专用验收：独立核算数据、文件与实例身份。"""

import io
import json
import time
from decimal import Decimal
from qoder_byoc_sandbox_example.settings import save
from .data import seed_csv, read_rows, selected, check_analysis, sha, atomic_json
from .shared import connect, results, worker_logs, emit


def input_integrity_checks(phase, rows, original_rows):
    if phase == "collect":
        return {"input_matches_downloaded_records": rows == original_rows}
    if phase == "append":
        record = rows[-1] if rows else {}
        return {
            "original_records_preserved": rows[:-1] == original_rows,
            "appended_record_matches_request": (
                len(rows) == len(original_rows) + 1
                and record.get("date") == "2026-10-29"
                and record.get("category") == "现场演示"
                and record.get("region") == "华东"
                and Decimal(record.get("amount", "0")) == Decimal("6666")
            ),
        }
    return {}


def verify(config, values, state, phase):
    if not state.get(phase + "_done"):
        raise ValueError("对应轮次尚未完成")
    box = connect(state, values)
    browser = connect(state, values, "browser_sandbox_id")
    dashboard = json.loads(box.files.read("/workspace/artifacts/dashboard.json"))
    csv = box.files.read("/workspace/input.csv", format="bytes")
    rows = read_rows(csv)
    transfer = json.loads(box.files.read("/workspace/evidence/transfer.json"))
    downloaded = browser.files.read(transfer["browser_path"], format="bytes")
    original_rows = selected(
        read_rows(seed_csv()),
        transfer["filters"]["month"],
        transfer["filters"]["region"],
    )
    check_analysis(dashboard, rows)
    logs = worker_logs(box)
    posted = {x["tool_use_id"] for x in logs if x["kind"] == "tool_posted"}
    executed = {x["tool_use_id"] for x in logs if x["kind"] == "tool_start"}
    identity = json.loads(box.files.read("/workspace/identity.json"))
    checks = {
        "different_instances": state["analysis_sandbox_id"]
        != state["browser_sandbox_id"],
        "browser_download_matches_source": read_rows(downloaded) == original_rows,
        "transfer_hashes_match": transfer["source_sha256"]
        == transfer["target_sha256"]
        == sha(downloaded),
        "source_unchanged": sha(box.files.read("/workspace/source.csv", format="bytes"))
        == state["source_sha256"],
        "input_hash_matches_dashboard": sha(csv) == dashboard["input_sha256"],
        "identity_matches": all(
            identity[k] == state[k]
            for k in ["run_id", "analysis_sandbox_id", "browser_sandbox_id"]
        ),
        "tool_results_delivered": bool(executed) and executed <= posted,
        "analysis_code_exists": len(box.files.read("/workspace/analysis.py")) > 50,
        "browser_screenshot_exists": len(
            box.files.read("/workspace/artifacts/browser.png", format="bytes")
        )
        > 1000,
        "published_and_checked": dashboard["independently_checked"] is True,
    }
    if state["analysis_mode"] == "custom_image":
        checks["custom_image_runtime_verified"] = (
            state.get("runtime_source_verified") is True
        )
        checks["service_ready_on_creation"] = (
            state.get("service_ready_without_setup") is True
        )
        checks["analysis_ingress_authenticated"] = (
            state.get("ingress_status", {}).get("anonymous") in {401, 403}
            and state.get("ingress_status", {}).get("authenticated") == 200
        )
    import openpyxl

    workbook_data = box.files.read("/workspace/artifacts/analysis.xlsx", format="bytes")
    book = openpyxl.load_workbook(
        io.BytesIO(workbook_data), read_only=True, data_only=True
    )
    excel = list(book["Data"].values)
    checks["excel_rows_match"] = len(excel) - 1 == dashboard["row_count"]
    checks["excel_total_matches"] = (
        abs(sum(float(r[3]) for r in excel[1:]) - dashboard["total_amount"]) < 0.005
    )
    book.close()
    expected_rows = selected(
        rows, excluded=dashboard.get("filters", {}).get("exclude_categories", [])
    )
    checks["exported_csv_matches"] = (
        read_rows(box.files.read("/workspace/artifacts/data.csv", format="bytes"))
        == expected_rows
    )
    checks["exported_json_matches"] = (
        json.loads(box.files.read("/workspace/artifacts/analysis.json")) == dashboard
    )
    exported = json.loads(box.files.read("/workspace/evidence/exports.json"))
    expected_names = {"data.csv", "analysis.xlsx", "analysis.json", "report.html"}
    checks["export_manifest_complete"] = {
        item["name"] for item in exported
    } == expected_names
    checks["export_hashes_match"] = checks["export_manifest_complete"]
    if checks["export_manifest_complete"]:
        for item in exported:
            actual = box.files.read(
                "/workspace/artifacts/" + item["name"], format="bytes"
            )
            if sha(actual) != item["sha256"] or len(actual) != item["bytes"]:
                checks["export_hashes_match"] = False
            if item["name"] == "report.html":
                checks["offline_plotly_embedded"] = (
                    b"plotly.js" in actual and len(actual) > 100000
                )
    if phase == "collect":
        checks["all_downloaded_rows_analyzed"] = dashboard["row_count"] == len(
            original_rows
        )
        checks["no_initial_exclusion"] = (
            dashboard.get("filters", {}).get("exclude_categories", []) == []
        )
    if phase == "revise":
        first = state["collect_snapshot"]
        largest = max(first["category_totals"], key=first["category_totals"].get)
        checks["highest_category_excluded"] = dashboard.get("filters", {}).get(
            "exclude_categories"
        ) == [largest]
        checks["csv_unchanged_after_question"] = sha(csv) == first["input_sha256"]
    if phase == "append":
        first = state["collect_snapshot"]
        checks["one_record_added"] = len(rows) == len(original_rows) + 1
        checks["amount_increased_by_6666"] = (
            abs(dashboard["total_amount"] - first["total_amount"] - 6666) < 0.005
        )
        checks["live_category_visible"] = (
            dashboard["category_totals"].get("现场演示") == 6666
        )
        checks["original_categories_restored"] = (
            dashboard.get("filters", {}).get("exclude_categories", []) == []
        )
    checks.update(input_integrity_checks(phase, rows, original_rows))
    snapshot = {
        "phase": phase,
        "checks": checks,
        "dashboard": dashboard,
        "verified_at": time.time(),
    }
    atomic_json(results(state) / (phase + "-verification.json"), snapshot)
    (results(state) / (phase + "-analysis.py")).write_text(
        box.files.read("/workspace/analysis.py")
    )
    (results(state) / "worker.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in logs)
    )
    (results(state) / "analysis.xlsx").write_bytes(workbook_data)
    (results(state) / "browser.png").write_bytes(
        box.files.read("/workspace/artifacts/browser.png", format="bytes")
    )
    emit(
        "verification",
        phase=phase,
        checks=checks,
        row_count=dashboard["row_count"],
        total_amount=dashboard["total_amount"],
        revision=dashboard["revision"],
    )
    if not all(checks.values()):
        raise RuntimeError(
            "验收失败：" + ", ".join(k for k, v in checks.items() if not v)
        )
    state[phase + "_verified"] = True
    state[phase + "_snapshot"] = dashboard
    save(state)
    if phase == "append":
        atomic_json(
            results(state) / "LIVE_PASS.json",
            {
                "run_id": state["run_id"],
                "analysis_mode": state["analysis_mode"],
                "all_phases_verified": True,
                "analysis_sandbox_id": state["analysis_sandbox_id"],
                "browser_sandbox_id": state["browser_sandbox_id"],
                "verified_at": time.time(),
            },
        )
        emit("LIVE_PASS", run_id=state["run_id"])

"""Readable terminal cards; presentation only, no task submission or cloud I/O."""
import os
import shutil
import sys
import unicodedata


ROUNDS = {
    "collect": ("1 / 3 · 首次分析", 100, 45392.18,
                "网页采集：筛选、翻页、下载 CSV → 报表分析：运行 Python、生成看板和文件。"),
    "revise": ("2 / 3 · 连续追问", 65, 28508.77,
               "Browser 不重新下载；Analysis 复用 input.csv，排除最高分类并更新结果。"),
    "append": ("3 / 3 · 现场追加", 101, 52058.18,
               "Analysis 恢复全部分类，追加 1 条 6,666，再运行 Python、更新所有交付文件。"),
}


class Cards:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.color = self.stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        self.width = max(28, min(92, shutil.get_terminal_size((84, 24)).columns - 6))

    def ink(self, text, color=""):
        codes = {"cyan":"96", "yellow":"93", "green":"92", "red":"91", "bold":"1", "dim":"2"}
        return f"\033[{codes[color]}m{text}\033[0m" if self.color and color else text

    def line(self, text="", color=""):
        print("  " + self.ink(str(text), color), file=self.stream, flush=True)

    def wrapped(self, text, width):
        for original in str(text).splitlines() or [""]:
            line, length = "", 0
            for char in original:
                size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
                if line and length + size > width:
                    yield line, length
                    line, length = "", 0
                line += char
                length += size
            yield line, length

    def paragraph(self, text, color=""):
        for line, _ in self.wrapped(text, self.width):
            self.line(line, color)

    def message(self, text):
        self.line("MESSAGE · 发给 Qoder 的提示词", "cyan")
        self.line("╭" + "─" * (self.width - 2) + "╮", "cyan")
        for line, width in self.wrapped(text, self.width - 4):
            self.line("│ " + line + " " * (self.width - 4 - width) + " │", "cyan")
        self.line("╰" + "─" * (self.width - 2) + "╯", "cyan")

    def intro(self, phase, message, status="new"):
        title, _, _, action = ROUNDS[phase]
        self.line()
        self.line("━" * self.width, "dim")
        self.line(title, "bold")
        self.line()
        self.message(message)
        if status != "new":
            self.line("本轮已有执行记录；下方展示配置内容，向导不会重复发送。", "dim")
        self.line()
        self.line("执行动作", "bold")
        self.paragraph(action)
        self.line()

    def result(self, event):
        checks = event.get("checks") or {}
        passed = sum(value is True for value in checks.values())
        ok = bool(checks) and passed == len(checks)
        phase = event.get("phase")
        count, amount = event.get("row_count"), event.get("total_amount")
        numeric = isinstance(amount, (int, float))
        amount_text = f"{amount:,.2f}" if numeric else str(amount)
        expected = ROUNDS.get(phase)
        matches = expected and count == expected[1] and numeric and abs(amount - expected[2]) < .005
        color = "green" if ok else "red"
        self.line()
        self.line("RESULT · 实际结果", "bold")
        self.line(f"{count} 条数据  ·  合计 {amount_text}  ·  看板 v{event.get('revision')}", color)
        facts = {
            "transfer_hashes_match":"浏览器下载与分析输入的 SHA-256 一致",
            "csv_unchanged_after_question":"input.csv 保持不变，无需重新下载",
            "highest_category_excluded":"已排除金额最高的分类",
            "one_record_added":"仅追加 1 条记录",
            "amount_increased_by_6666":"与首轮相比，金额只增加 6,666",
            "original_records_preserved":"原有记录完整保留",
        }
        for key, description in facts.items():
            if key == "transfer_hashes_match" and phase != "collect":
                continue
            if checks.get(key) is True:
                self.line("✓ " + description)
        if checks.get("excel_rows_match") is True and isinstance(count, int):
            self.line(f"✓ Excel：{count} 条数据 + 1 行表头 = {count + 1} 行")
        if ok:
            verdict = "✓ 符合预期" if matches else "✓ 独立验收通过"
            self.line(f"{verdict} · {passed}/{len(checks)} 项检查通过", "green")
            if expected and not matches:
                self.line("实际数字与默认演示场景不同，请结合当前配置查看。", "yellow")
        else:
            self.line(f"✗ 未通过验收 · {passed}/{len(checks)} 项通过", "red")
            failed = [name for name, value in checks.items() if value is not True]
            self.paragraph("未通过：" + ("、".join(failed) or "未收到验收项"), "red")
        self.line("─" * self.width, "dim")

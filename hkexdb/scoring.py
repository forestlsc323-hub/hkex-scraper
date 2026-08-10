"""字段级准确率：拿抽出来的结果和人工答案表逐字段对。

你的路线图把这件事列成阶段 0，理由是「没有它算不了准确率」——
对。没有标准答案，「跑通了」和「跑对了」区分不开，而这两件事差很远。

用法：
  1. 程序会生成 data/answer_key_template.csv，列和 deals.csv 一样；
  2. 你把已经人工核过的那些单填进去（只填你确定的字段，其余留空）；
  3. 存成 data/answer_key.csv，跑一次，出 准确率报告.txt。

空格＝这个字段没有标准答案，**不计分**，不是「答案是空」。
所以你可以只填有把握的那几列，先把类型和要约价的准确率量出来。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

# 主键：同一家公司可能被不同要约人先后发要约（绿科×2、天鸽 MGO+PO），
# 所以绝不能按代码去重 —— 这是你踩过的坑。
KEY_COLUMNS = ("股票代码", "公告日期")

# 数值字段比对时允许的相对误差（印刷四舍五入造成的尾差不算错）
_NUMERIC_FIELDS = {"要约价(HKD)", "主值溢价率(%)", "交易规模(HKD)",
                   "隐含股权价值(HKD)", "市净率P/B", "每股NAV"}
_TOLERANCE = 0.005


def _key(row: dict) -> tuple:
    return tuple(str(row.get(c, "")).strip() for c in KEY_COLUMNS)


def _num(value: str):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def matches(field: str, got: str, want: str) -> bool:
    got, want = str(got or "").strip(), str(want or "").strip()
    if field in _NUMERIC_FIELDS:
        a, b = _num(got), _num(want)
        if a is not None and b is not None:
            return abs(a - b) <= max(_TOLERANCE, abs(b) * _TOLERANCE)
    return got == want


@dataclass
class FieldScore:
    field: str
    graded: int = 0        # 答案表里填了值的条数
    right: int = 0
    wrong: list = field(default_factory=list)   # (key, 抽到的, 应该是)

    @property
    def rate(self) -> float:
        return self.right / self.graded if self.graded else 0.0


@dataclass
class Report:
    scores: list
    missing_deals: list     # 答案表里有、程序没抽到的单
    extra_deals: list       # 程序抽到、答案表里没有的单（不算错，只列出来）

    def text(self) -> str:
        lines = ["字段级准确率", "=" * 56, ""]

        # ⚠️ 漏检那一节必须无条件走到。一单都没对上时 scores 是空的，
        # 而那恰恰是**全部漏检**的情形 —— 早退等于把最严重的结果吞掉。
        if not self.scores:
            lines.append("没有任何字段被计分（答案表全空，或者一单都没对上）。")
        else:
            lines.append(f"{'字段':<22}{'对/计分':>10}{'准确率':>9}")
            lines.append("-" * 56)
            for s in sorted(self.scores, key=lambda x: x.rate):
                lines.append(
                    f"{s.field:<22}{s.right:>4}/{s.graded:<5}{s.rate * 100:>8.1f}%")

            lines += ["", "错在哪里", "-" * 56]
            any_wrong = False
            for s in self.scores:
                for key, got, want in s.wrong:
                    any_wrong = True
                    lines.append(f"[{s.field}] {'/'.join(key)}")
                    lines.append(f"    抽到：{got or '（空）'}")
                    lines.append(f"    应为：{want}")
            if not any_wrong:
                lines.append("（没有错项）")

        if self.missing_deals:
            lines += ["", f"答案表里有、但程序没抽到的 {len(self.missing_deals)} 单",
                      "-" * 56]
            lines += [f"  {'/'.join(k)}" for k in self.missing_deals]
            lines.append("  ↑ 这些是漏检，比抽错更严重 —— 先查筛查层放没放它们过。")
        if self.extra_deals:
            lines += ["", f"程序抽到、答案表里没有的 {len(self.extra_deals)} 单"
                          "（不算错，可能是你还没填）", "-" * 56]
            lines += [f"  {'/'.join(k)}" for k in self.extra_deals[:30]]
        return "\n".join(lines)


def score(got_rows: list[dict], answer_rows: list[dict]) -> Report:
    """逐字段比对。答案表里留空的格子不计分。"""
    got_by_key = {_key(r): r for r in got_rows}
    scores: dict[str, FieldScore] = {}
    missing = []

    for want_row in answer_rows:
        key = _key(want_row)
        got_row = got_by_key.get(key)
        if got_row is None:
            if any(str(v).strip() for k, v in want_row.items()
                   if k not in KEY_COLUMNS):
                missing.append(key)
            continue
        for field_name, want in want_row.items():
            if field_name in KEY_COLUMNS or not str(want).strip():
                continue          # 空格＝没有标准答案，不计分
            s = scores.setdefault(field_name, FieldScore(field_name))
            s.graded += 1
            got = got_row.get(field_name, "")
            if matches(field_name, got, want):
                s.right += 1
            else:
                s.wrong.append((key, str(got), str(want)))

    answer_keys = {_key(r) for r in answer_rows}
    extra = [k for k in got_by_key if k not in answer_keys]
    return Report(list(scores.values()), missing, extra)


def write_template(columns: list[str], path: Path) -> Path:
    """生成空的答案表，列和 deals.csv 完全一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        w.writerow([f'="{"0" * 5}"' if c == "股票代码" else "" for c in columns])
    return path


def load(path: Path) -> list[dict]:
    from .dealsview import load_rows
    return load_rows(path)

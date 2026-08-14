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


def _hint(got: str, want: str) -> str:
    """两个数差在哪儿，如果差法本身有名字的话就说出来。

    错项列表最费时间的部分不是「哪里错了」，而是「谁错了」—— 是程序
    抽错，还是答案表录错。有几类差法一眼就能定性，直接标出来能省掉
    一次翻原文：实跑里 08439 和 01657 两条最后确认是答案表打错的。
    """
    a, b = _num(got), _num(want)
    if a is None or b is None or a == b:
        return ""
    if a == -b:
        return ("两边数值一样、符号相反 —— 要么答案表那格正负号写反了，"
                "要么程序把溢价/折让判反了")
    if b and abs(a / b - 10) < 0.01:
        return "程序的数正好是答案的 10 倍 —— 多半是某一边少写/多写了一位"
    if a and abs(b / a - 10) < 0.01:
        return "答案正好是程序的 10 倍 —— 多半是某一边少写/多写了一位"
    x, y = str(int(abs(a))), str(int(abs(b)))
    if len(x) == len(y):
        diff = [i for i in range(len(x)) if x[i] != y[i]]
        if len(diff) == 1:
            return (f"位数相同，只有第 {diff[0] + 1} 位不一样"
                    f"（{x[diff[0]]} / {y[diff[0]]}）—— 像是按错一个键")
        if sorted(x) == sorted(y):
            return "两个数由同一批数字组成 —— 像是打字时换了位"
    return ""


@dataclass
class FieldScore:
    field: str
    graded: int = 0        # 答案表里填了值的条数
    right: int = 0
    wrong: list = field(default_factory=list)   # (key, 抽到的, 应该是, 版本, 配到哪一行)

    @property
    def rate(self) -> float:
        return self.right / self.graded if self.graded else 0.0


@dataclass
class Report:
    scores: list
    missing_deals: list     # 答案表里有、程序没抽到的单
    extra_deals: list       # 程序抽到、答案表里没有的单（不算错，只列出来）
    # 按「同代码、日期最近」配上的（不是同一天）。放宽了配对就必须
    # 逐条报出来 —— 一个会自己放宽的比对器，如果不说，比严格的更危险。
    loose_pairs: list = field(default_factory=list)
    stale: int = 0          # 参与评分的行里，有几行是旧版本抽取器留下的
    version: str = ""       # 当前抽取器版本

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
                for key, got, want, ver, paired in s.wrong:
                    any_wrong = True
                    lines.append(f"[{s.field}] {'/'.join(key)}")
                    lines.append(f"    抽到：{got or '（空）'}")
                    # ⚠️ 「抽错了」和「配错行了」看起来一模一样，而修法完全
                    # 相反。同一个标的先后做过好几单要约时（00195 綠科三单），
                    # 答案的那一天可能被配到另一单上去 —— 不把配到的日期
                    # 印出来，就会去改一个根本没错的正则。
                    if paired and paired[1]:
                        lines.append(
                            f"    ↳ 这一行配的是程序里 {paired[0]} 那一单，"
                            f"相隔 {paired[1]} 天 —— 同一标的多单时先看是不是配错了行")
                    lines.append(f"    应为：{want}")
                    hint = _hint(got, want)
                    if hint:
                        lines.append(f"    ↳ {hint}")
                    if ver and self.version and ver != self.version:
                        lines.append(
                            f"    ⚠ 这一行是旧版本 {ver} 抽的（当前 {self.version}）"
                            f"—— 它没跑过新规则，先重跑再看这条")
            if not any_wrong:
                lines.append("（没有错项）")

        if self.stale:
            lines += ["",
                      f"⚠️ 参与评分的行里有 {self.stale} 行是**旧版本**抽取器留下的",
                      "-" * 56,
                      "   存档是跨次累积的，只有落在本次日期范围里的那些行会被重抽。",
                      "   范围外的行还是老规则抽出来的结果 —— 拿它们算准确率，",
                      "   量的是历史，不是现在的规则。想全部刷新，把日期范围放到",
                      "   覆盖它们（存档命中不发请求，只是重抽 PDF）。"]

        if self.loose_pairs:
            lines += ["", f"按「同代码、日期最近」配上的 {len(self.loose_pairs)} 单",
                      "-" * 56]
            for key, got_date, gap in sorted(self.loose_pairs,
                                             key=lambda x: -x[2]):
                lines.append(f"  {'/'.join(key)}　←→　程序 {got_date}"
                             f"（差 {gap} 天）")
            lines.append("  ↑ 你记的是 T0，程序打开的是那一单里最早的留存公告，"
                         "未必同一天。")
            lines.append("    差得太多的要人工确认一下是不是同一单。")

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


# 同一单交易，你答案表里记的是 T0 的日期，程序打开的可能是几天后的
# 后续公告 —— 日期对不上，但讲的是同一单。差这么多天以内就认为是一单。
NEAR_DAYS = 45


def _date(row: dict):
    import datetime as _dt
    text = str(row.get("公告日期", "")).strip()
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _filled(row: dict) -> int:
    """这一行填了几个正经字段。用来在日期打平时挑更完整的那行。"""
    return sum(1 for k, v in row.items()
               if k not in KEY_COLUMNS and str(v or "").strip())


def pair_up(got_rows: list[dict], answer_rows: list[dict],
            near_days: int = NEAR_DAYS):
    """把程序抽的和答案表按「同代码、日期最近」配对。

    ⚠️ 原来是拿 (代码, 公告日期) 精确配的，于是这份报告里 13 单被算成
    「漏检」—— 而其中至少 8 单**程序明明抽到了**，只是日期差几天：

        01780  答案 05-15，程序 05-07（差 8 天）
        01953  答案 04-22，程序 04-24（差 2 天）
        02362  答案 05-27，程序 05-22（差 5 天）

    你记的是 T0，程序打开的是那一单里最早的**留存**公告，两者未必同一天。
    结果同一单被同时记成一次漏检和一次多余，准确率的分母凭空缩水一半。
    量错了的准确率比没有准确率更糟：它会指挥我去修不存在的问题。

    同一家公司可能有两单不同的要约（02362 就是 MGO + PO），所以按代码
    分组之后仍要一对一配，每个程序行只能被用掉一次。
    """
    # 两边的代码都过一遍规范化 —— deals.csv 里也有「01117<br/>01432」
    # 这种双代码的行（镜像归档），不统一就配不上。
    pool: dict[str, list] = {}
    for row in got_rows:
        # 双代码的行挂在两个代码下面，配上哪个都算
        for code in codes_in(row.get("股票代码", "")) or [""]:
            pool.setdefault(code, []).append(row)
    used: set[int] = set()

    pairs, misses = [], []
    for want in sorted(answer_rows, key=lambda r: str(r.get("公告日期", ""))):
        code = normalise_code(want.get("股票代码", ""))
        want_date = _date(want)
        best, best_gap, best_rank = None, None, None
        for cand in pool.get(code, []):
            if id(cand) in used:
                continue
            gap = 0
            cand_date = _date(cand)
            if want_date and cand_date:
                gap = abs((cand_date - want_date).days)
                if gap > near_days:
                    continue
            # ⚠️ 「判定＝要约」的行排在最前，比日期近更重要。
            # 01102 環能國際：程序在 2025-02-07 抽出了完整的一行
            # （MGO 0.05 -20.38% 29,787,139），另有一行 2025-02-27 是
            # 「待核」（打开的是延迟寄发公告，什么都没有）。答案记的是
            # 02-27，于是配到了空的那一行，报告上显示「溢价率抽到（空）」——
            # 而程序其实完全答对了，只是配错了行。
            #
            # 「待核／非要约」是程序自己说的「这行不成立」，拿它去参加评分
            # 等于用一行已知无效的数据判自己错。
            rank = (0 if str(cand.get("判定", "")).strip() in ("", "要约")
                    else 1, gap, -_filled(cand))
            if best_rank is None or rank < best_rank:
                best, best_gap, best_rank = cand, gap, rank
        if best is None:
            misses.append(want)
        else:
            used.add(id(best))
            pairs.append((want, best, best_gap or 0))

    seen_left: set[int] = set()
    leftovers = []
    for rows in pool.values():
        for row in rows:
            if id(row) in used or id(row) in seen_left:
                continue
            seen_left.add(id(row))
            leftovers.append(row)
    return pairs, misses, leftovers


def score(got_rows: list[dict], answer_rows: list[dict]) -> Report:
    """逐字段比对。答案表里留空的格子不计分。"""
    pairs, miss_rows, leftovers = pair_up(got_rows, answer_rows)
    scores: dict[str, FieldScore] = {}
    missing = [_key(r) for r in miss_rows
               if any(str(v).strip() for k, v in r.items()
                      if k not in KEY_COLUMNS)]
    loose = [(_key(w), b.get("公告日期", ""), gap)
             for w, b, gap in pairs if gap]

    for want_row, got_row, _gap in pairs:
        key = _key(want_row)
        for field_name, want in want_row.items():
            if field_name in KEY_COLUMNS or not str(want).strip():
                continue          # 空格＝没有标准答案，不计分
            s = scores.setdefault(field_name, FieldScore(field_name))
            s.graded += 1
            got = got_row.get(field_name, "")
            if matches(field_name, got, want):
                s.right += 1
            else:
                s.wrong.append((key, str(got), str(want),
                                str(got_row.get("抽取器版本", "")).strip(),
                                (str(got_row.get("公告日期", "")).strip(), _gap)))

    extra = [_key(r) for r in leftovers]
    # 旧版本抽的行拿来算准确率，量的是历史不是现在的规则 —— 必须说出来。
    from .store import EXTRACTOR_VERSION
    stale = sum(1 for _w, g, _gap in pairs
                if str(g.get("抽取器版本", "")).strip()
                and str(g.get("抽取器版本", "")).strip() != EXTRACTOR_VERSION)
    return Report(list(scores.values()), missing, extra, loose,
                  stale=stale, version=EXTRACTOR_VERSION)


def write_template(columns: list[str], path: Path) -> Path:
    """生成空的答案表，列和 deals.csv 完全一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        w.writerow([f'="{"0" * 5}"' if c == "股票代码" else "" for c in columns])
    return path


# 答案表的列名别名。你自己那张表用的是投行习惯的叫法，
# deals.csv 用的是这个程序的叫法 —— 两边对不上就一条都配不上，
# 而报告只会显示「一单都没对上」，看不出是列名的问题。
#
# 与其让你每次导出都手工改一遍表头，不如程序认得这些叫法。
_ALIASES = {
    "股份代码": "股票代码", "股份代號": "股票代码", "代码": "股票代码",
    "股份代码（规范）": "股票代码", "股份代码(规范)": "股票代码",
    "首次公告日期": "公告日期", "T0": "公告日期", "日期": "公告日期",
    "溢价率": "主值溢价率(%)", "溢價率": "主值溢价率(%)",
    "主值溢价率": "主值溢价率(%)",
    "交易规模": "交易规模(HKD)", "交易規模": "交易规模(HKD)",
    "要约价": "要约价(HKD)", "要約價": "要约价(HKD)",
    "要约类": "要约类型", "类型": "要约类型",
    "公司简称": "受要约方", "公司名称": "受要约方",
}


def normalise_code(value: str) -> str:
    """股票代码统一成 5 位。

    同一家公司在你表里可能写 195 / 0195.HK / 00195，在 deals.csv 里是
    00195 —— 不统一的话它们是三个不同的键，一单都配不上。
    """
    text = str(value or "").strip().upper()
    text = text.split(".HK")[0].split("<")[0].strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(5)[-5:] if digits else ""


def codes_in(value: str) -> list[str]:
    """一行里所有的股票代码。

    deals.csv 里镜像归档那些行是「01117<br/>01432」这种双代码 ——
    一行同时讲两家公司，所以它应该能配上其中**任何一个**。
    """
    import re as _re
    seen, out = set(), []
    for chunk in _re.split(r"[^0-9A-Za-z.]+", str(value or "")):
        code = normalise_code(chunk)
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def normalise_row(row: dict) -> dict:
    """把一行答案表整理成程序认得的样子：列名、代码、百分号。"""
    out: dict = {}
    for key, value in row.items():
        name = _ALIASES.get(str(key).strip(), str(key).strip())
        # 别名撞车时不覆盖已有的正式列（表里同时有「股票代码」和「股份代码」）
        if name in out and str(out[name]).strip():
            continue
        out[name] = value
    if out.get("股票代码"):
        out["股票代码"] = normalise_code(out["股票代码"])
    for col in ("主值溢价率(%)", "交易规模(HKD)", "要约价(HKD)"):
        if col in out:
            out[col] = str(out[col] or "").replace("%", "").replace(",", "").strip()
    return out


def load(path: Path) -> list[dict]:
    from .dealsview import load_rows
    return [normalise_row(r) for r in load_rows(path)]


# ---------------------------------------------------------------- 答案表自检

def sanity_check(answer_rows: list[dict]) -> list[str]:
    """答案表自己也会有错 —— 你手册里「公告会错」那一节同样适用于人工表。

    这里只查算术上不可能的值，不碰口径判断：
    折让不可能超过 100%（价格再低也只能低到 0）。
    """
    problems = []
    for row in answer_rows:
        pct = _num(row.get("主值溢价率(%)", ""))
        if pct is not None and pct < -100:
            problems.append(
                f"{row.get('股票代码', '?')} / {row.get('公告日期', '?')}："
                f"折让 {pct}% 在算术上不可能（价格最低只能到 0，即 -100%）。"
                f"多半是正负号写反了。")
    return problems

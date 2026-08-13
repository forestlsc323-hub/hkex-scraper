"""孤儿单：一整单交易的公告全被灰掉了，于是这单凭空消失。

铁律二说「分类层的错误是静默污染」，而这一层是它最狠的一种表现：
每一条单看都灰得对，合起来却丢掉一整单交易。

    06113 UTS      索引里 4 条：延遲寄發 / 寄發綜合文件 / 回應文件 / 要約結果
    02350 數科      索引里 5 条：寄發 / 綜合文件 / 回應文件 / 澄清 / 規則3.7澄清
    01102 環能國際  用户先后给了三份 PDF，全是后续公告，没有一份是 T0

判据只有一条，而且它是**演绎**不是猜测：

    后续公告的存在，证明存在过一份 T0。

「寄發綜合要約文件」「延遲寄發」「要約結果」这些标题在讲的是**某一个要约**
的后续动作 —— 有子必有父。所以一个聚类里只要出现了后续公告，却没有任何一条
进了留存桶，这单交易就是被整单丢掉了。

这不是「可能漏了」，是「一定漏了」。**这张表的长度就是召回率的缺口。**

⚠️ 这一层只做**报告**，不改任何桶。要不要捞回来、怎么捞，是下一步的事 ——
先把「到底漏了多少」量出来，才谈得上修。
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

# 同一标的两条公告隔这么多天就算两单不同交易（和抓取层同一个口径）。
GAP_DAYS = 60

# 「这是某个要约的后续动作」——这些词一出现，就意味着有一份 T0 存在过。
# 故意收得比 screening_rules 的排除词表窄：那张表还管着「月度更新」
# 「股東特別大會」这类和要约无关也会出现的东西，拿它们当「有父」的证据
# 会把一堆无关公告认成孤儿单。
_FOLLOW_UP = re.compile(
    r"寄發|延遲寄發|綜合文件|綜合要約文件|回應文件|"
    r"要約結果|要約結束|接納水平|接納程度|成為無條件|"
    r"截止|失效|派付代價|撤回接納|修訂要約|提高要約價|"
    r"獨立財務顧問(?:意見|之建議)|獨立董事委員會函件")

# 但光有后续词还不够 —— 「寄發二零二五年年報」也命中「寄發」。
# 必须同时看得出这是**要约**的后续。
_ABOUT_AN_OFFER = re.compile(r"要約|收購|私有化|要约|收购")

RETAINED = "retained"


@dataclass
class Orphan:
    """一单被整单丢掉的交易。"""

    code: str
    name: str
    first: str
    last: str
    rows: list = field(default_factory=list)     # 这一单的全部公告

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def evidence(self) -> str:
        """最能说明「这单存在过」的那条标题。"""
        for row in self.rows:
            title = row.get("title", "")
            if _FOLLOW_UP.search(title) and _ABOUT_AN_OFFER.search(title):
                return title
        return self.rows[0].get("title", "") if self.rows else ""


def _day(text: str):
    try:
        return dt.date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _clusters(rows: list, gap_days: int) -> list[list]:
    """同一标的的公告按时间间隔切成若干单。"""
    rows = sorted(rows, key=lambda r: str(r.get("date", "")))
    out: list[list] = []
    prev = None
    for row in rows:
        day = _day(row.get("date", ""))
        if out and prev is not None and day is not None and \
                (day - prev).days > gap_days:
            out.append([])
        elif not out:
            out.append([])
        if day is not None:
            prev = day
        out[-1].append(row)
    return out


def find_orphans(screened: list, gap_days: int = GAP_DAYS) -> list[Orphan]:
    """找出「有后续公告、却一条都没留存」的单。

    `screened` 是筛查层的行（含 code / date / title / verdict.bucket）。
    """
    by_code: dict[str, list] = {}
    for row in screened:
        code = str(row.get("code", "")).strip()
        if not code:
            continue          # 没有代码的归不到单上，另有报告管它们
        by_code.setdefault(code, []).append(row)

    out: list[Orphan] = []
    for code, rows in sorted(by_code.items()):
        for cluster in _clusters(rows, gap_days):
            if any(_bucket(r) == RETAINED for r in cluster):
                continue      # 有 T0 留存，不是孤儿
            proof = [r for r in cluster
                     if _FOLLOW_UP.search(r.get("title", ""))
                     and _ABOUT_AN_OFFER.search(r.get("title", ""))]
            if not proof:
                continue      # 没有「有父」的证据，不算孤儿
            days = [d for d in (_day(r.get("date", "")) for r in cluster) if d]
            out.append(Orphan(
                code=code,
                name=next((r.get("name", "") for r in cluster if r.get("name")), ""),
                first=min(days).isoformat() if days else "",
                last=max(days).isoformat() if days else "",
                rows=list(cluster)))
    out.sort(key=lambda o: (o.first, o.code))
    return out


def _bucket(row: dict) -> str:
    verdict = row.get("verdict")
    if verdict is not None and hasattr(verdict, "bucket"):
        return verdict.bucket
    return str(row.get("bucket", ""))


def write_report(orphans: list[Orphan], root: Path) -> Path:
    """落一张盘，好逐单查。"""
    out_dir = root / "data" / "screening"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "orphans.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["股票代码", "名称", "最早", "最晚", "公告数",
                    "证明这单存在过的标题", "桶", "全部标题"])
        for o in orphans:
            w.writerow([o.code, o.name, o.first, o.last, o.count, o.evidence,
                        "／".join(sorted({_bucket(r) for r in o.rows})),
                        "｜".join(r.get("title", "")[:60] for r in o.rows)])
    return path


def summary(orphans: list[Orphan]) -> list[str]:
    """写进运行日志的几行话。"""
    if not orphans:
        return ["  孤儿单：0 —— 每一单交易都至少留存了一份公告。"]
    lines = [f"  ⚠️ 孤儿单 {len(orphans)} 单：这些单的公告**全部**被灰掉了，"
             f"于是整单从成品表里消失",
             "     （判据不是猜的：有「寄發綜合文件／要約結果」这类后续公告，"
             "就一定存在过一份 T0）"]
    for o in orphans[:8]:
        lines.append(f"     {o.code} {o.name}　{o.first}~{o.last}　"
                     f"{o.count} 条　{o.evidence[:46]}")
    if len(orphans) > 8:
        lines.append(f"     …还有 {len(orphans) - 8} 单，见 data/screening/orphans.csv")
    lines.append("     这张表的长度就是召回率的缺口 —— 比抽错严重得多。")
    return lines


# ---------------------------------------------------------------- 第二个缺口
#
# 4921 条落在「人工复核」桶 —— 那是**默认桶**：保留词和排除词都没命中的
# 全落这儿。没人会去看 4921 条，所以它实际上等于丢掉。
#
# 但它们不都是要约。下面这条只做**测量**，不改任何桶：数一数里面有多少条
# 标题长得像 T0。它回答的是「捞回来这件事值不值得做、要做多大」，
# 而不是「这条到底是不是」。
#
# 判据故意写得比保留词表严：必须同时有「要约的类型措辞」和「现金要约」
# 这类 T0 才会有的说法，而且不能带后续公告的词。

_T0_SHAPE = re.compile(
    r"強制性|强制性|自願|自愿|部[分份](?:公開)?(?:收購)?要約|全面(?:性)?[^，。；]{0,6}要約|"
    r"mandatory|voluntary|partial\s+offer", re.I)
_CASH_OFFER = re.compile(r"現金要約|现金要约|收購要約|全面要約|cash\s+offer", re.I)


def looks_like_t0(title: str) -> bool:
    """标题长得像不像一份 T0。**只用于测量**，不用于判定。"""
    if _FOLLOW_UP.search(title):
        return False
    return bool(_T0_SHAPE.search(title) and _CASH_OFFER.search(title))


def manual_bucket_gap(screened: list) -> tuple[int, list]:
    """「人工复核」桶里有多少条标题像 T0。返回 (条数, 前几条标题)。

    这个桶是默认桶：两层词表都没命中的全落这儿。没人会去看几千条，
    所以它实际上等于丢掉 —— 而这个数字就是「捞回来值不值得做」的答案。
    """
    hits = [r for r in screened
            if _bucket(r) == "manual" and looks_like_t0(r.get("title", ""))]
    return len(hits), [r.get("title", "")[:60] for r in hits[:6]]


def gap_summary(screened: list, orphans: list) -> list[str]:
    """两个缺口一起报：整单丢掉的，和压在人工桶里没人看的。"""
    lines = summary(orphans)
    total, samples = manual_bucket_gap(screened)
    if not total:
        return lines
    lines.append(f"  ⚠️ 「人工复核」桶里有 {total} 条标题**看起来像 T0**"
                 f"（只是测量，没有改判）：")
    for title in samples:
        lines.append(f"     {title}")
    lines.append("     那个桶是默认桶（两层词表都没命中的全落这儿），"
                 "几千条没人看得完，")
    lines.append("     所以它实际上等于丢掉。这个数字决定了「捞回来」"
                 "这件事值不值得做。")
    return lines

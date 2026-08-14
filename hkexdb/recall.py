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

# 但光有后续词还不够。第一版用「要約|收購|私有化」当判据，实跑 9657 条
# 报出 105 单，逐条看下来只有 11 单像真漏检 —— 那张表当场就废了。
# 拆开看 105 单是什么：
#     75 单  证据标题里根本没有「要約」两个字（收购资产、认购新股、
#            发通函……「收購」在港股公告里太常见，不能当要约的证据）
#     12 单  協議安排／第86條 私有化（不是三种要约，本来就在范围外）
#      5 单  清洗豁免
#      2 单  股份回购
# 所以判据收紧成：**证据标题必须出现「要約」二字**。
_ABOUT_AN_OFFER = re.compile(r"要約|要约")

# 这几类整个就不在课题范围内（screening 的 special_species 也是这么分的）。
# 一条命中，整单不算孤儿 —— 它不是漏了，是本来就不收。
_OUT_OF_SCOPE = re.compile(
    r"協議安排|计划安排|計劃安排|第\s*86\s*條|第\s*673\s*條|scheme\s*of\s*arrangement|"
    r"股份購回|購回|回購|"
    r"清洗豁免|whitewash|"
    # 债券／票据要约不是股份要约（01030 新城發展「以現金購買其尚未償還的
    # 2025年到期4.625%有擔保優先票據」）
    r"優先票據|優先note|債券|票據", re.I)

# 同一标的在这前后这么多天里已经留存过一份 T0 —— 那这一簇后续公告
# 只是那一单的尾巴被 60 天间隔切开了，不是丢了一单。
# 01749 杉杉品牌和 01217 中國創新投資实跑就是这么被误报的。
NEAR_RETAINED_DAYS = 180

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
        kept = [d for d in (_day(r.get("date", "")) for r in rows
                            if _bucket(r) == RETAINED) if d]
        for cluster in _clusters(rows, gap_days):
            if any(_bucket(r) == RETAINED for r in cluster):
                continue      # 有 T0 留存，不是孤儿
            # ⚠️ 范围外只看**那条证据本身**，不看整簇。一簇里混进一条
            #    「購回股份」的无关公告，不该把整单交易一起判出局
            #    （01939 東京中央拍賣第一版就是这么被误杀的）。
            proof = [r for r in cluster
                     if _FOLLOW_UP.search(r.get("title", ""))
                     and _ABOUT_AN_OFFER.search(r.get("title", ""))
                     and not _OUT_OF_SCOPE.search(r.get("title", ""))]
            if not proof:
                continue      # 没有「有父」的证据，不算孤儿
            days = [d for d in (_day(r.get("date", "")) for r in cluster) if d]
            if kept and days and min(
                    abs((k - d).days) for k in kept for d in days
            ) <= NEAR_RETAINED_DAYS:
                continue      # 同一单的尾巴被 60 天间隔切开了，不是丢了一单
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


# ---------------------------------------------------------------- 把它捞回来
#
# 上面那张表只回答「漏了多少」。这一段回答「怎么捞」。
#
# 捞的依据不是换个词表 —— 换词表是在猜，而且改一个词会牵动全市场 9657 条。
# 依据是**结构**：这一簇里有后续公告，就一定存在过一份 T0，那份 T0 就在
# 这一簇里（或者压根没抓到，那是抓取层的事，这里也要说清楚）。
# 所以捞回来这件事只剩一个问题：**簇里哪一条是那份 T0**。
#
# ⚠️ 捞回来的行不进留存桶，进一个单独的「捞回」桶，并且原样带着
# 「它本来被判成什么、被哪个词判的」。铁律二：分类层的改动必须看得见，
# 不能悄悄多出几行来。

RESCUED = "rescued"

# T0 的签名：**提出**一个要约。后续公告会把要约全称复述一遍，但不会说
# 「提出」——它们说的是「寄發」「延遲」「結果」。
_T0_VERB = re.compile(r"提出|作出|提呈")
_T0_KIND = re.compile(r"強制性|强制性|自願|自愿|部[分份].{0,4}要約|全面.{0,6}要約|"
                      r"無條件.{0,6}要約|有條件.{0,6}要約")

# 纯程序动作。带这些词的那一条不管怎么复述要约全称，都不是 T0。
_PROCEDURAL = re.compile(
    r"寄發|延遲|綜合文件|接納表格|過戶表格|通知信函|回條|"
    r"要約結果|要約截止|接納水平|成為無條件|結算|"
    r"翌日披露|月報表|規則\s*22|交易披露")


def _t0_score(row: dict) -> tuple:
    """这一条像不像那份 T0。排序用，越小越像。

    三档，档内按日期从早到晚（T0 总是一簇里最早的那一条）：
      0  说了「提出…要約」，而且不带任何程序动作的词 —— 就是它
      1  说了「提出…要約」，但同一条里还带着程序动作（打包公告）
      2  剩下的
    """
    title = row.get("title", "")
    signed = bool(_T0_VERB.search(title) and _T0_KIND.search(title))
    if signed:
        tier = 1 if _PROCEDURAL.search(title) else 0
    else:
        tier = 2
    return (tier, str(row.get("date", "")))


# 一簇里最多打开几份来试。标题排序只是**猜**哪一份是 T0，猜错了就得换
# 下一份 —— 判准归正文，不归标题（见 rescue_ranked）。
RESCUE_TRIES = 3


def rescue_ranked(orphan: Orphan, limit: int = RESCUE_TRIES) -> list:
    """一簇孤儿公告里，最像 T0 的那几份，从像到不像排好。

    ⚠️ 为什么给**一队**而不是一个：标题排序只能猜。01145 勇利投資那一簇里
    四条标题都写着「提出自願性有條件全面現金要約」，靠标题分不出哪条是
    T0、哪条是寄发通知；实跑挑中的那份打开一看，正文里连「要約價」都没有。

    所以这一层只负责**排队**，判准交给正文：打开第一份，抽不出要约字段
    就换下一份，直到有一份抽得出来或者队列用完。这和交易规模那道闸是
    同一条教训 —— 闸要当筛子用，不是开关。

    空队列**不是失败**，是另一种答案：那份 T0 根本没被抓到
    （02350 數科的要約期間 2025-04-29 就开始了，可簇里最早的一条是
    05-23 —— 04-29 那份压根不在列表里，是抓取层漏的，不是筛查层）。
    这两种缺口得分开报，修法完全不同。
    """
    ranked = sorted(orphan.rows, key=_t0_score)
    return [r for r in ranked if _t0_score(r)[0] < 2][:limit]


def rescue(orphan: Orphan) -> dict | None:
    """队首那一份。只给报告用 —— 真正开哪一份由正文说了算。"""
    ranked = rescue_ranked(orphan, 1)
    return ranked[0] if ranked else None


def rescue_all(orphans: list[Orphan], limit: int = RESCUE_TRIES
               ) -> tuple[list, list]:
    """返回 (捞回来的 [(orphan, 候选队列)], 簇里根本没有 T0 的 orphan)。"""
    found, empty = [], []
    for o in orphans:
        queue = rescue_ranked(o, limit)
        (found.append((o, queue)) if queue else empty.append(o))
    return found, empty


def write_report(orphans: list[Orphan], root: Path) -> Path:
    """落一张盘，好逐单查。

    ⚠️ 标题**不截断**。第一版截到 60 字，结果几条 T0 在报告里看着
    像该留的，实跑却被灰掉了 —— 杀死它的那个词正好在第 60 字之后。
    报告的用处就是告诉人「是哪个词干的」，截断把这个用处砍掉了。
    """
    out_dir = root / "data" / "screening"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "orphans.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["股票代码", "名称", "最早", "最晚", "公告数",
                    "证明这单存在过的标题", "桶",
                    "捞回来的那一条（日期）", "捞回来的那一条（标题）",
                    "它本来在哪个桶", "被哪个词判的", "全部标题"])
        for o in orphans:
            pick = rescue(o)
            w.writerow([
                o.code, o.name, o.first, o.last, o.count, o.evidence,
                "／".join(sorted({_bucket(r) for r in o.rows})),
                (pick or {}).get("date", ""),
                (pick or {}).get("title", ""),
                _bucket(pick) if pick else "",
                "、".join(_killed_by(pick)) if pick else "",
                "｜".join(r.get("title", "") for r in o.rows)])
    return path


def _killed_by(row: dict) -> list:
    """这一条是被哪些词判出局的。看得见才改得动。"""
    verdict = row.get("verdict")
    if verdict is None:
        return []
    return list(getattr(verdict, "matched_exclude", None) or [])


def summary(orphans: list[Orphan]) -> list[str]:
    """写进运行日志的几行话。"""
    if not orphans:
        return ["  孤儿单：0 —— 每一单交易都至少留存了一份公告。"]
    lines = [f"  ⚠️ 孤儿单 {len(orphans)} 单：这些单的公告**全部**被灰掉了，"
             f"于是整单从成品表里消失",
             "     （判据不是猜的：有「寄發綜合文件／要約結果」这类后续公告，"
             "就一定存在过一份 T0）"]
    found, empty = rescue_all(orphans)
    for o, queue in found[:8]:
        row = queue[0]
        lines.append(f"     {o.code} {o.name}　{o.first}~{o.last}　"
                     f"{o.count} 条　排队开 {len(queue)} 份，先开 "
                     f"{row.get('date', '')}"
                     f"（原判 {_bucket(row)}"
                     + (f"，被「{'、'.join(_killed_by(row))}」判的" if _killed_by(row) else "")
                     + f"）：{row.get('title', '')[:36]}")
    for o in empty[:4]:
        lines.append(f"     {o.code} {o.name}　{o.first}~{o.last}　"
                     f"{o.count} 条　⚠️ 这一簇里**没有**T0 —— "
                     f"那份公告压根没抓到，是抓取层的缺口")
    if len(found) + len(empty) > 12:
        lines.append(f"     …其余见 data/screening/orphans.csv")
    lines.append(f"     捞得回来 {len(found)} 单（筛查层判错），"
                 f"捞不回来 {len(empty)} 单（抓取层没抓到）—— 两种缺口修法不同。")
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

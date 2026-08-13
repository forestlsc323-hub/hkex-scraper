"""流程执行器：把整条流水线包成一个可被界面调用的东西。

界面（app.py）只负责画窗口和转发消息，真正的逻辑全在这里 ——
这样界面那层薄到几乎不可能出错，而这里可以离线测试。

对外只有一个 `run()`：
    · 通过 `on_log` 回调把每一行输出交给界面
    · 通过 `on_step` 回调报告当前进度
    · `cancel_event` 置位后尽快停下
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEPS = ["抓取公告列表", "质控筛查", "抽取要约字段", "生成网页", "打包诊断"]


class Cancelled(Exception):
    """用户点了停止。"""


# 两个维度的中文写法。标签（MGO/VGO/PO）是这两列合成出来的：
#     部分 → PO；全面＋强制 → MGO；全面＋自愿 → VGO
OBLIGATION_LABEL = {"mandatory": "强制", "voluntary": "自愿"}
SCOPE_LABEL = {"full": "全面", "partial": "部分"}

VERDICT_LABEL = {"offer": "要约", "unclear": "待核", "not_offer": "非要约",
                 "mirror": "镜像重复", "duplicate": "同单重复"}

# 一份公告最多占用多久（给用户看的上界，真正的硬闸在 pdf_source 里）
MAX_SECONDS_PER_PDF = 90

# 同一个标的最多展开几份公告。
#
# 实跑 71 份里，09638 法拉帝一家占了 19 份（27%）—— 全是同一单 PO 的
# 后续公告，而你的答案表里它只有一单。一单交易只有一个 T0，其余都是
# 程序公告；把它们逐份下下来，等于花 27% 的时间去下不需要的东西。
#
# 取最早的几份（T0 一定在最前），4 份的余量足够覆盖「同一标的先后
# 两单不同交易」的情形 —— 金川國際在你答案表里就有 MGO 和 PO 各一单。
MAX_PER_TARGET = 4


# 同一家公司的两份公告隔了这么久，就当成**两单不同的交易**。
#
# 一单要约从 T0 到收官通常三四个月，中间的进展公告隔几天到几周；
# 而同一家公司的前后两单要约往往隔一年以上。60 天是个偏保守的切法：
# 切多了只是多下几份 PDF，切少了会整单丢掉 —— 代价完全不对等。
#
# 02362 金川就是活例子：2026-03 一单 MGO，2026-05 一单 PO，隔 86 天。
NEW_DEAL_GAP_DAYS = 60

# 服务端一次最多吐这么多条，再调大 rowRange 也没用（照抄 asso 的常数）。
SERVER_RECORD_CAP = 10000


def _cluster_key(rows: list, gap_days: int) -> list:
    """把一个标的的公告按时间间隔切成若干单。返回 [(第几单, row), …]。"""
    out, cluster, prev = [], 0, None
    for row in rows:
        day = None
        try:
            day = dt.date.fromisoformat(str(row.get("date", ""))[:10])
        except ValueError:
            pass
        if prev is not None and day is not None and (day - prev).days > gap_days:
            cluster += 1
        if day is not None:
            prev = day
        out.append((cluster, row))
    return out


def _cap_per_target(rows: list) -> tuple[list, dict]:
    """同一标的的**同一单**交易只展开最早的几份，其余先放着。

    返回 (要抽的, {代码: 跳过几份})。跳过的**不是删除** —— 它们仍在
    筛查表里，只是这一轮不下载。需要时把 max_per_target 调大重跑。

    ⚠️ 「同一单」这三个字是这一版补上的，而它在跨年份抓取时是**必需**的。
    原来是按股票代码在整个日期范围里数，抓一年还看不出问题；一旦抓
    2024~2026，一家公司 2024 年那单的四份公告会把名额全占掉，
    2026 年那单**一份都打不开**，而且日志上只显示「跳过 N 份」，
    看不出丢掉的是一整单交易。用户的目标是任意年份都能爬，
    所以这不是优化，是修 bug。
    """
    cfg = _listing_config()
    cap, gap = MAX_PER_TARGET, NEW_DEAL_GAP_DAYS
    try:
        cap = int(cfg.get("max_per_target", cap))
        gap = int(cfg.get("new_deal_gap_days", gap))
    except (TypeError, ValueError):
        pass

    by_code: dict = {}
    for row in sorted(rows, key=lambda r: (r.get("date", ""), r.get("row_id", ""))):
        by_code.setdefault(row.get("code") or row.get("row_id"), []).append(row)

    keep, skipped = [], {}
    for code, group in by_code.items():
        seen: dict = {}
        for cluster, row in _cluster_key(group, gap):
            seen[cluster] = seen.get(cluster, 0) + 1
            if seen[cluster] <= cap:
                keep.append(row)
            else:
                skipped[code] = skipped.get(code, 0) + 1
    keep.sort(key=lambda r: (r.get("date", ""), r.get("row_id", "")))
    return keep, skipped


def _normalise(text: str) -> str:
    import re as _re
    return _re.sub(r"\s+", "", text or "")


def mark_mirror_filings(deals: list) -> int:
    """同一份联合公告在双方代码下各归档一次，合并记一单（坑⑨）。

    实跑年初至今就撞上两组：
        03336 巨騰國際 / 06613 藍思科技      要约方＝藍思科技股份有限公司
        01875 東曜藥業 / 02268 藥明合聯      要约方＝藥明合聯生物技術有限公司
    两边抽出来的数字一模一样，直接进表就是把同一单记了两遍 ——
    做中位数时这一单的权重凭空翻倍。

    谁是要约方？**公告自己说了**：把已抽出的「要约方」名字和两家的
    简称比一下，对得上的那一行就是要约方自己的归档，标成镜像重复。
    对不上就两行都留着交人工 —— 手册说这一步不许自动猜受要约方。
    """
    groups: dict[tuple, list] = {}
    for d in deals:
        groups.setdefault(("题", d.date, _normalise(d.title)), []).append(d)
        # 再按**抽出来的数字**分一次组。同日同标题这个键太脆：实跑里
        # 两组镜像明明都在表内，却只标出来一组 —— 双方各自归档时标题
        # 可能差一个字（代号后缀、括号里的英文名），日期也可能差一天。
        #
        # 而「要约价、交易规模、要约方三样完全相同」是比标题强得多的
        # 证据：两行讲的就是同一单。
        if d.offer_price and d.deal_size and d.offeror:
            groups.setdefault(("数", d.offer_price, d.deal_size,
                               _normalise(d.offeror)), []).append(d)

    marked = 0
    seen: set[int] = set()
    for group in groups.values():
        group = [d for d in group if id(d) not in seen]
        if len(group) < 2 or len({d.code for d in group}) < 2:
            continue
        offeror = _normalise(next((d.offeror for d in group if d.offeror), ""))
        if not offeror:
            continue
        for d in group:
            name = _normalise(d.name)
            # 简称出现在要约方全称里 → 这一行是要约方自己的归档
            if name and len(name) >= 2 and name in offeror:
                d.verdict = "mirror"
                d.verdict_reason = (
                    f"与另一条重复；本行的公司「{d.name}」就是要约方，"
                    f"受要约方是同组另一条。合并记一单（坑⑨）")
                seen.add(id(d))
                marked += 1
    return marked


def mark_duplicate_filings(deals: list) -> int:
    """同一单交易被两份文件各记一遍，留最早那份。

    一单要约通常至少两份文件写着完整字段：先出**联合公告**，几周后出
    **综合文件**。两份的要约价、溢价、规模一模一样 —— 直接进表就是把
    同一单记了两遍，做中位数时权重凭空翻倍。

    实跑 2025 全年撞上一批：
        02442 怡俊集團  24 页的公告 + 80 页的综合文件，两行完全相同
        00195 綠科 / 01428 耀才 / 01863 中國龍天 / 02623 愛德  同理

    和镜像归档（坑⑨）不是一回事：那个是**同一份**公告在双方代码下各
    归档一次，这个是**同一单**交易的两份不同文件。判据也不同 ——
    这里要求同代码、同要约价、同规模。

    留最早那份，因为 T0 才是这单的日期。后面那份标出来但不删（软删除，
    铁律二）—— 综合文件里有公告没有的东西（时间表、独立意见），
    人可能正想看它。
    """
    groups: dict[tuple, list] = {}
    for d in deals:
        if d.verdict != "offer":
            continue
        # ⚠️ 键里**不能带溢价率**。两份文件写的是同一单，但抽出来的东西
        # 可能一多一少：00372 保德那单，公告那份有 -2.23%，综合文件那份
        # 溢价是空的。把溢价放进键里，这两行就永远配不上 ——
        # 于是既没合并，打分时还可能配到空的那一行去。
        # 同代码 + 同要约价 + 同规模已经足够断定是同一单。
        groups.setdefault((d.code, d.offer_price, d.deal_size), []).append(d)

    marked = 0
    for key, group in groups.items():
        if len(group) < 2 or not key[0] or not key[1]:
            continue
        group.sort(key=lambda d: (d.date or "9999", d.news_id))
        keeper, rest = group[0], group[1:]
        filled = _fill_blanks_from(keeper, rest)
        for d in rest:
            d.verdict = "duplicate"
            d.verdict_reason = (
                f"与 {keeper.date} 那份是同一单（要约价、规模相同），"
                f"多半是先公告、后综合文件。已合并记一单，此行不重复计数")
            marked += 1
        if filled:
            keeper.notes = "；".join(
                x for x in (keeper.notes,
                            f"这些字段取自同一单的另一份文件：{'、'.join(filled)}") if x)
    return marked


# 可以跨同一单的两份文件互补的字段。
# 出处（evidence）跟着一起过来，所以铁律三没有破 —— 引文还是那句原话，
# 只是它印在这单的另一份文件上。补了哪几个字段会写进备注。
_FILLABLE = ("premium_pct", "premium_basis", "deal_size", "offer_price",
             "offeror", "offeror_fa", "target_full", "total_shares",
             "nav_per_share", "six_month_low", "six_month_high",
             "last_trading_day", "listing_intent", "consideration")


def _fill_blanks_from(keeper, others: list) -> list[str]:
    """同一单的两份文件信息互补，把空着的格子补上。返回补了哪几个字段。

    00372 保德那单：公告那份抽到溢价 -2.23%，综合文件那份溢价是空的。
    两份讲的是同一单，人看的时候当然会把它们合起来读 ——
    程序没理由摆一个空格子在那里。
    """
    filled = []
    for name in _FILLABLE:
        if str(getattr(keeper, name, "") or "").strip():
            continue
        for other in others:
            value = str(getattr(other, name, "") or "").strip()
            if value:
                setattr(keeper, name, value)
                filled.append(name)
                break
    if not keeper.premium_ladder:
        for other in others:
            if other.premium_ladder:
                keeper.premium_ladder = dict(other.premium_ladder)
                filled.append("premium_ladder")
                break
    return filled


@dataclass
class Deal:
    """一单要约的最终结果 —— 这就是你要的那张表的一行。"""

    news_id: str = ""                 # 披露易的公告号，存档的主键
    # 正文层判定：标题层留下来的，未必真是要约（实跑 15 条里只有 2 条是）
    verdict: str = "unclear"
    verdict_reason: str = ""
    # 当事方 —— 做 precedent 时第一眼看的就是「谁买谁、谁做的 FA」
    code: str = ""                    # 受要约方股票代码
    name: str = ""                    # 受要约方（披露易归属的简称）
    target_full: str = ""             # 受要约方全称（标题里写了才有）
    offeror: str = ""                 # 要约方
    offeror_fa: str = ""              # 要约方财务顾问
    date: str = ""                    # 首次公告日期（T0）
    last_trading_day: str = ""        # 停牌前最后交易日
    # 条款
    offer_type: str = ""
    obligation_basis: str = ""        # 强制 / 自愿
    offer_scope: str = ""             # 全面 / 部分
    consideration: str = ""           # 现金 / 证券 / 现金＋证券
    offer_price: str = ""
    price_headline: str = ""          # 替代方案里含递延结算的那个价（如果有两套）
    premium_pct: str = ""             # 主值溢价率
    premium_basis: str = ""           # 主值口径 —— 没有它这个数字没意义
    premium_ladder: dict = field(default_factory=dict)   # 全部比较项
    deal_size: str = ""
    listing_intent: str = ""          # 拟维持上市 / 拟撤销上市
    is_conditional: str = ""          # 无条件MGO＝已成事实；有条件＝还要判断能否成
    board: str = ""                   # 主板 / GEM —— GEM 单可比性弱
    nature: str = ""                  # 交易性质（建议值，待人工确认）
    nature_reasons: str = ""
    # 估值组：和 deal size 分开。付给公众股东的才是规模，整家公司作价多少是估值
    total_shares: str = ""
    nav_per_share: str = ""
    implied_equity_value: str = ""    # 已发行股数 × 要约价
    pb_ratio: str = ""                # 要约价 ÷ 每股NAV
    runup_pct: str = ""               # 未受干扰日→最后交易日涨幅（泄露证据）
    six_month_low: str = ""
    six_month_high: str = ""
    # 出处与复核
    confidence: str = ""
    checks: str = ""
    pdf_url: str = ""
    title: str = ""
    notes: str = ""
    evidence: dict = field(default_factory=dict)
    # 只给日志看的：翻了几页 / 共几页。不进 CSV，是排查用的，
    # 「哪一份把时间吃掉了」得能一眼看出来。
    pages_note: str = ""


@dataclass
class Result:
    ok: bool = False
    fetched: int = 0
    screened: int = 0
    deals: list = field(default_factory=list)
    report_path: Path | None = None
    diagnostic_path: Path | None = None
    log_path: Path | None = None
    error: str = ""
    buckets: dict = field(default_factory=dict)
    qc_notes: list = field(default_factory=list)


DEFAULT_KEYWORDS = ["要約", "收購", "私有化"]


def _listing_config() -> dict:
    from .config import section
    return section("config.yaml", root=ROOT)


def _speed_settings() -> tuple[int, int, str, list[str]]:
    """从 config.yaml 读抓取旋钮。读不到就用实测过的默认值。"""
    cfg = _listing_config()
    try:
        return (int(cfg.get("row_range_step", 4000)),
                max(1, int(cfg.get("max_workers", 4))),
                str(cfg.get("mode", "keyword")),
                list(cfg.get("title_keywords") or DEFAULT_KEYWORDS))
    except (TypeError, ValueError):
        return 4000, 4, "keyword", list(DEFAULT_KEYWORDS)


def _keep_raw_files() -> bool:
    """要不要把公告原件在本地留一份。默认不留（用完即弃）。"""
    return bool(_listing_config().get("keep_raw_files", False))


# 先翻几页再决定要不要翻完。0 = 关掉这个机制，老老实实整份解析。
PROBE_PAGES = 12

# 开几个解析子进程。见 _parse_workers 的实测数据。
PARSE_WORKERS = "auto"


def _probe_pages() -> int:
    """侦察页数。要约公告的封面必然印着价钱或名目，12 页留足了余量。"""
    from .config import section
    try:
        return max(0, int(section("config.yaml", key="pdf", root=ROOT)
                          .get("probe_pages", PROBE_PAGES)))
    except (TypeError, ValueError, AttributeError):
        return PROBE_PAGES


def _auto_workers() -> int:
    """按这台机器的核数定解析子进程数。

    实测（8 份 60 页）：1 个 17.3 秒 / 2 个 8.8 秒 / 3 个 6.6 秒 /
    4 个 4.7 秒 —— 接近线性。原来默认写死 2，在四核以上的机器上等于
    白扔一半速度；写死 4 又会在双核机上把界面拖卡。

    取核数的一半、封顶 4：一份 295 页的综合文件解析时能吃掉几百兆，
    四个并发就是一两个 G，再多不值当。
    """
    import os
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _parse_workers() -> int:
    """开几个解析子进程。config.yaml 写 auto 或留空就按核数自动定。"""
    from .config import section
    try:
        want = section("config.yaml", key="pdf", root=ROOT).get("parse_workers",
                                                                PARSE_WORKERS)
    except (TypeError, AttributeError):
        want = PARSE_WORKERS
    if isinstance(want, str) and want.strip().lower() in ("auto", ""):
        return _auto_workers()
    try:
        return max(1, min(8, int(want)))
    except (TypeError, ValueError):
        return _auto_workers()


def _parse_timeout() -> float:
    """一份公告最多允许解析多久。到点子进程会被真的杀掉。"""
    from .config import section
    try:
        return max(10.0, float(section("config.yaml", key="pdf", root=ROOT)
                               .get("parse_timeout_seconds",
                                    MAX_SECONDS_PER_PDF)))
    except (TypeError, ValueError, AttributeError):
        return float(MAX_SECONDS_PER_PDF)


def _premium_rules() -> dict:
    """溢价率主值的口径，从 config.yaml 读。

    这一节的配置以前只是写在那儿给人看的，代码根本没读 —— 也就是说
    「改配置就能改行为」那句话对它不成立。锚点优先级恰恰是有争议的
    一项（08439 你要最后交易日，3336 你要未受干扰日），所以它必须是
    一个你改得动的旋钮，而不是埋在 .py 里的常量。
    """
    from . import selectors
    from .config import section

    out = {"window": selectors.DEFAULT_WINDOW,
           "anchor_priority": selectors.DEFAULT_ANCHOR_PRIORITY,
           "fallback": selectors.WINDOW_FALLBACK}
    try:
        cfg = section("config.yaml", key="primary_value_rules",
                      root=ROOT).get("premium") or {}
    except (TypeError, AttributeError):
        return out
    if isinstance(cfg.get("window"), str):
        out["window"] = cfg["window"]
    order = cfg.get("anchor_priority")
    if isinstance(order, list) and all(isinstance(x, str) for x in order) and order:
        out["anchor_priority"] = tuple(order)
    ladder = cfg.get("window_fallback")
    if isinstance(ladder, list) and all(isinstance(x, str) for x in ladder):
        # 空列表＝关掉退档：只认 30 日，别的一律留空
        out["fallback"] = tuple(ladder)
    return out


def _month_chunks(d1: dt.date, d2: dt.date) -> list[tuple[dt.date, dt.date]]:
    """按月切段。照抄你 asso 那份文件 search_by_category 的做法，
    连理由都一样：「类别筛选后记录数远小于上限，无需按天」。

    关键词筛过之后一个月也就几十条，按天切纯属浪费请求。
    """
    out, cur = [], d1
    while cur <= d2:
        nxt = (dt.date(cur.year + 1, 1, 1) if cur.month == 12
               else dt.date(cur.year, cur.month + 1, 1))
        out.append((cur, min(d2, nxt - dt.timedelta(days=1))))
        cur = nxt
    return out


def _fetch_by_keyword(client, vendor_config, keywords: list[str],
                      d1: dt.date, d2: dt.date, log, on_step,
                      cancel_event) -> dict[str, dict]:
    """让披露易在服务端就把标题筛掉，别把全市场拉回来自己筛。

    检索页那个「標題」输入框对应的就是 servlet 的 `title` 参数 ——
    你 asso 的文件里一直显式传 `"title": ""`（不带关键词＝抓全量），
    那个空字符串就是这条路的入口。

    差距有多大：一周全量是 6993 条、7 次请求；按关键词是几十条、
    每个关键词一次请求。一年从约 250 次请求降到十几次。

    【注意】这是**漏检风险最高**的一处改动，所以：
      · 关键词取并集，一条公告命中任一即收；
      · screening_rules.yaml 里那六条已核实的真实 T0 标题，
        必须条条命中至少一个关键词，否则测试直接不让跑；
      · 万一某个关键词一条都没返回，说明服务端语义和预期不符，
        调用方会退回全量抓取 —— 宁可慢，不可漏。
    """
    import json as _json

    found: dict[str, dict] = {}
    chunks = _month_chunks(d1, d2)
    total = len(chunks) * len(keywords)
    done = 0
    per_keyword: dict[str, int] = {k: 0 for k in keywords}

    cap = int(vendor_config.ROW_RANGE_STEP)

    def ask(a: dt.date, b: dt.date, kw: str, row_range: int | None = None) -> list:
        """问一段日期，**问到接口说「没有下一页」为止**。

        ⚠️ 原来一个 (月, 关键词) 只发一个请求、rowRange=500、不翻页 ——
        某个月要是超过 500 条，多出来的一声不响就没了，而且日志上一切
        正常：那一段照样显示「返回 500 条」，没人看得出后面还有。
        （asso 那份客户端的 search_by_category 是同一个写法，同一个洞；
        它按天抓的 _search_single_day 反而是对的。）

        接口自己会说话，用它的话最可靠：
            hasNextRow    还有没有下一页
            loadedRecord  这次实际返回多少条
        这个接口没有 offset，翻页靠**把 rowRange 调大重查一遍**
        （照抄 _search_single_day）。调到服务端硬顶 10000 还说有下一页，
        就把日期对半劈开 —— 一直劈到单日，那时只能如实报警。
        """
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        row_range = row_range or cap
        params = {
            "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
            "market": "SEHK", "stockId": "-1", "documentType": "-1",
            "fromDate": a.strftime("%Y%m%d"), "toDate": b.strftime("%Y%m%d"),
            "title": kw, "searchType": "0",
            "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
            "rowRange": str(row_range), "lang": "zh",
        }
        resp = client.session.get(vendor_config.HKEX_SEARCH_URL, params=params,
                                  timeout=vendor_config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        raw = payload.get("result")
        records = _json.loads(raw) if raw not in (None, "null") else []
        time.sleep(vendor_config.SLEEP_BETWEEN_REQUESTS)

        if not payload.get("hasNextRow"):
            return records
        if row_range < SERVER_RECORD_CAP:
            bigger = min(row_range + cap, SERVER_RECORD_CAP)
            log(f"      {a}~{b}「{kw}」还有下一页，上限调到 {bigger} 重查")
            return ask(a, b, kw, bigger)
        if a < b:
            mid = a + (b - a) // 2
            log(f"      {a}~{b}「{kw}」已到服务端硬顶 {SERVER_RECORD_CAP}，"
                f"劈成两段重查")
            return ask(a, mid, kw) + ask(mid + dt.timedelta(days=1), b, kw)
        log(f"      【注意】{a}「{kw}」单日就超过服务端硬顶 "
            f"{SERVER_RECORD_CAP} 条，这一天可能有漏 —— 请人工核一下")
        return records

    for c1, c2 in chunks:
        for kw in keywords:
            records = ask(c1, c2, kw)
            new = 0
            for rec in records:
                nid = rec["NEWS_ID"]
                if nid not in found:
                    found[nid] = client._clean(rec)
                    new += 1
            per_keyword[kw] += len(records)

            done += 1
            log(f"  [{done}/{total}] {c1:%Y-%m}　「{kw}」返回 {len(records)} 条，"
                f"新增 {new}　累计 {len(found)}")
            on_step(0, done / max(1, total))

    dead = [k for k, n in per_keyword.items() if n == 0]
    if dead:
        raise KeywordModeUnusable(
            f"关键词 {dead} 一条都没返回 —— 服务端的标题筛选语义和预期不符")
    return found


class DateFilterBug(RuntimeError):
    """抓回来一堆、按日期筛完一条不剩。这只可能是解析错了，必须炸。"""


class KeywordModeUnusable(RuntimeError):
    """关键词模式看着不对劲。宁可退回慢的全量抓取，也不能静默漏掉公告。"""


def _fetch(d1: dt.date, d2: dt.date, log, on_step, cancel_event) -> list[dict]:
    """用 vendor/hkex_client.py（用户提供的实战客户端，一字未改）抓列表。

    客户端本身不改，但它的两个速度旋钮在 vendor/config.py 里写死了
    （步长 500、单线程）。实测 7 天要 10 分钟，一年就是 5 小时 ——
    这两个值从我们的 config.yaml 覆盖进去，客户端代码仍然一字未动。
    """
    import sys
    sys.path.insert(0, str(ROOT / "vendor"))
    import config as vendor_config              # noqa: E402
    from hkex_client import HKEXClient          # noqa: E402

    step, workers, mode, keywords = _speed_settings()
    vendor_config.ROW_RANGE_STEP = step

    n_days = (d2 - d1).days + 1
    workers = max(1, min(workers, n_days))      # 段数不能多过天数

    log(f"日期范围 {d1} ~ {d2}（{n_days} 天）")
    log("正在访问检索页建立会话…")

    client = HKEXClient()
    cookies = sorted(c.name for c in client.session.cookies)
    log(f"会话 cookie：{cookies if cookies else '（服务端未下发）'}")

    if mode == "keyword":
        log(f"抓取方式：关键词 {keywords}（服务端筛标题，按月分段）")
        try:
            found = _fetch_by_keyword(client, vendor_config, keywords,
                                      d1, d2, log, on_step, cancel_event)
            out = sorted(found.values(),
                         key=lambda r: r.get("DATE_TIME", ""), reverse=True)
            log(f"关键词模式抓到 {len(out)} 条")
            return out
        except Cancelled:
            raise
        except Exception as exc:
            if type(exc).__name__ == "CancelledError":
                raise Cancelled() from exc
            # 宁可慢，不可漏 —— 关键词模式一有异常就退回全量
            log(f"【注意】关键词模式不可用（{type(exc).__name__}: {exc}）")
            log("   已自动退回全量抓取。慢，但不会漏。")

    log(f"抓取方式：全量（翻页步长 {step}，并发 {workers} 段，"
        f"共用一个限速器，总频率不变）")

    done = [0]
    lock = threading.Lock()

    def progress(day, total_days, count):
        # 【注意】并发时这个回调由多个线程调用，而且客户端把它包在
        # try/except 里 —— 这里抛异常会被吞掉，所以停止不能靠抛异常，
        # 只能靠 cancel_event（客户端每天开头都会检查它）。
        with lock:
            done[0] += 1
            n = done[0]
        log(f"  [{n}/{total_days}] {day}　累计 {count} 条")
        on_step(0, n / max(1, total_days))

    try:
        return client.search(d1, d2, progress_cb=progress,
                             cancel_event=cancel_event, max_workers=workers)
    except Exception as exc:
        # 客户端有自己的取消异常，名字不同但意思一样。不翻译的话，
        # 用户点了「停止」会看到一条像是崩溃的报错。
        if type(exc).__name__ == "CancelledError":
            raise Cancelled() from exc
        raise


def self_check(d1: dt.date, d2: dt.date, *, on_log=None,
               cancel_event=None, fetch_full=None, fetch_keyword=None) -> str:
    """同一段日期，两种抓法各跑一遍，逐条对 NEWS_ID。

    关键词模式快得多，但它的正确性取决于服务端怎么理解 `title` 参数 ——
    那是我在这里验证不了的。所以给你一个按钮：跑一次，把关键词模式
    漏掉的公告逐条列出来。漏的是无关公告就放心用；漏了要约公告，
    就把 config.yaml 的 mode 改回 full，并告诉我漏了什么。

    只对一小段日期跑（一两周），因为全量那一侧本来就慢。
    """
    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(str(text))
        if on_log:
            on_log(str(text))

    from . import screening as S

    step, workers, _mode, keywords = _speed_settings()
    log(f"自检：{d1} ~ {d2}")
    log(f"关键词 {keywords}")
    log("")

    def noop(*_a, **_k):
        return None

    log("【1/2】关键词模式")
    kw = (fetch_keyword or _fetch)(d1, d2, log, noop, cancel_event)
    log("")
    log("【2/2】全量模式（慢，请等）")
    full = (fetch_full or _fetch)(d1, d2, log, noop, cancel_event)

    kw_ids = {r.get("NEWS_ID") for r in kw}
    missed = [r for r in full if r.get("NEWS_ID") not in kw_ids]

    log("")
    log("=" * 56)
    log(f"关键词模式 {len(kw)} 条　全量 {len(full)} 条　"
        f"关键词漏掉 {len(missed)} 条")

    rules = S.load_rules("screening_rules.yaml")
    risky = []
    for rec in missed:
        verdict = S.classify_title(rec.get("TITLE", ""), rules)
        if verdict.bucket in (S.RETAINED, S.MANUAL):
            risky.append((verdict.bucket, rec))

    if not risky:
        log("漏掉的全是筛查层本来就会剔除的公告 —— 关键词模式可以放心用。")
    else:
        log(f"【注意】漏掉的里面有 {len(risky)} 条筛查层会留下来的，逐条列出：")
        for bucket, rec in risky[:50]:
            log(f"   [{bucket}] {rec.get('STOCK_CODE', '')} "
                f"{rec.get('TITLE', '')[:70]}")
        log("")
        log("→ 把 config.yaml 里 listing.mode 改成 full，并把这段发给 Claude。")
    log("=" * 56)

    out = ROOT / "自检报告.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


def probe_categories(on_log=None, fetch_html=None) -> str:
    """勘察披露易的公告分类码。

    你 asso 的 search_by_category 把架子搭好了，缺的就是 t2code 的值。
    拿到它，服务端就能直接给你「收購及合併」类的公告，连关键词模式
    带回来的那 113 条/月普通交易公告都不会回来。

    只读不写：读到什么报什么，读不到就说读不到并给出手工拿码的步骤。
    绝不猜一个码填进去 —— 猜错是静默漏掉整类公告。
    """
    from . import categories

    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(str(text))
        if on_log:
            on_log(str(text))

    if fetch_html is None:
        import sys
        sys.path.insert(0, str(ROOT / "vendor"))
        import config as vendor_config          # noqa: E402
        from hkex_client import HKEXClient      # noqa: E402

        client = HKEXClient()                   # 建会话，拿 cookie

        def fetch_html(url):
            resp = client.session.get(url, timeout=vendor_config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text

    log("正在读检索页的分类树…")
    cats, hits, report = categories.probe(fetch_html)
    for line in report.splitlines():
        log(line)

    out = ROOT / "类别勘察.txt"
    detail = list(lines)
    if cats:
        detail += ["", "=" * 56, "读到的全部分类", "=" * 56]
        detail += [f"  {c}" for c in cats]
    out.write_text("\n".join(detail), encoding="utf-8")
    log("")
    log(f"完整清单已写入 {out.name}")
    return str(out)


CACHE_DIR = "data/cache/pdf"

# 会长胖的目录，各自是什么、删了会怎样。
# 存档（data/store）不在这里 —— 那是这个工具的本体，删不得。
DISPOSABLE = [
    ("data/cache/pdf", "公告原件副本", "重跑时要重新下载"),
    ("data/cache", "接口响应缓存", "重跑时要重新请求"),
    ("data/raw", "列表原样副本", "存档 listing.csv 里有同样的内容"),
    ("data/screening", "筛查过程文件", "重跑筛查就会重新生成"),
    ("data/probe", "勘察产物", "点「勘察类别码」会重新生成"),
    ("logs", "运行日志", "只影响事后翻旧账"),
]


def disposable_report() -> list[tuple[str, str, int, int, str]]:
    """各个可删目录占了多少。返回 [(相对路径, 名称, 份数, 字节, 删了会怎样)]。

    上一轮用户问「你之前下载的 pdf 是不是都删掉了」—— 那时只能答
    data/cache/pdf 一个目录。实际上会长胖的不止它一个，
    列在一起才知道到底占了多少地方。
    """
    seen: set[Path] = set()
    out = []
    for rel, label, cost in DISPOSABLE:
        path = ROOT / rel
        if not path.exists():
            continue
        files = [f for f in path.rglob("*") if f.is_file() and f not in seen]
        if not files:
            continue
        seen.update(files)
        out.append((rel, label, len(files), sum(f.stat().st_size for f in files),
                    cost))
    return out


def clear_disposable(rels: list[str]) -> tuple[int, int]:
    """删掉指定的可删目录。返回 (删了几个文件, 腾出多少字节)。

    只删 DISPOSABLE 里列出来的路径 —— 传进来一个不在名单上的目录
    直接忽略，免得哪天一个笔误把 data/store 端了。
    """
    allowed = {rel for rel, _, _ in DISPOSABLE}
    gone = freed = 0
    for rel in rels:
        if rel not in allowed:
            continue
        path = ROOT / rel
        if not path.exists():
            continue
        for f in sorted(path.rglob("*"), key=lambda p: -len(p.parts)):
            if f.is_file():
                freed += f.stat().st_size
                f.unlink()
                gone += 1
    return gone, freed


def cache_info() -> tuple[int, int]:
    """公告原件副本占了多少地方。返回 (份数, 字节数)。

    这些副本不是「顺手存的」，是你工程要求里那条「原始文件永久保留，
    解析与抽取幂等可重跑」—— 公告一旦被替换或撤下，没有副本就再也
    复现不出当初抽的数字，审计链断在这里。

    实测每份 700 KB 上下：年初至今约 90 MB，2024-2026 三年约 280 MB。
    占的是硬盘不是内存；跑的时候同时只有并发数那么几份在内存里。
    """
    path = ROOT / CACHE_DIR
    if not path.exists():
        return 0, 0
    files = [f for f in path.iterdir() if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def score_against_answer_key(on_log=None) -> str:
    """拿 data/deals.csv 和 data/answer_key.csv 逐字段对，出准确率报告。

    没有标准答案，「跑通了」和「跑对了」区分不开 —— 而这两件事差很远。
    答案表里留空的格子不计分，所以你可以只填有把握的那几列。
    """
    from . import dealsview, scoring

    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(str(text))
        if on_log:
            on_log(str(text))

    from . import store
    archived = store.load_deals(ROOT)
    got = (list(archived.values()) if archived
           else dealsview.load_rows(ROOT / "data" / "deals.csv"))
    key_path = ROOT / "data" / "answer_key.csv"
    if not key_path.exists():
        tpl = scoring.write_template(DEAL_COLUMNS,
                                     ROOT / "data" / "answer_key_template.csv")
        log("还没有答案表。已经生成空模板：")
        log(f"  {tpl}")
        log("")
        log("把你人工核过的那些单填进去（只填你确定的字段，其余留空不计分），")
        log("另存为 data/answer_key.csv，再点一次这个按钮。")
    elif not got:
        log("data/deals.csv 是空的 —— 先跑一次抓取。")
    else:
        answers = scoring.load(key_path)
        for problem in scoring.sanity_check(answers):
            log(f"【注意】答案表本身有问题：{problem}")
        if scoring.sanity_check(answers):
            log("")
        report = scoring.score(got, answers)
        for line in report.text().splitlines():
            log(line)
        # 「漏了 4 单」不指挥任何动作，「这 4 单漏在索引层」才指挥得动。
        from . import trace as trace_mod
        for line in trace_mod.missing_report(got, answers):
            log(line)

    out = ROOT / "准确率报告.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


def _fetch_incrementally(d1: dt.date, d2: dt.date, log, on_step,
                         cancel_event, fetch, force: bool = False) -> list[dict]:
    """只抓存档里还没有的那些天，其余直接从存档取。

    你说的那个用法：抓过 2026 全年之后再要 2025-01-01 到今天，
    2026 那段一个请求都不发，只补 2025 和最近这几天。

    存档按「抓取方式＋关键词」分开记覆盖范围 —— 关键词模式抓过的
    日子不等于全量模式也抓过，口径不同混在一起会造成静默漏检。
    """
    from . import store

    _step, _workers, mode, keywords = _speed_settings()
    key = store.coverage_key(mode, keywords)
    total_days = (d2 - d1).days + 1
    if force:
        log("已勾选「忽略存档重新抓取」—— 这段日期全部重抓。")
        missing = store._days(d1, d2)
    else:
        missing = store.missing_days(ROOT, d1, d2, key)

    if not missing:
        rows = store.listing_between(ROOT, d1, d2)
        log(f"这段日期（{total_days} 天）存档里全都有，一个请求都不用发。")
        log(f"从存档取回 {len(rows)} 条公告。")
        log("（想强制重抓，把 data/store/coverage.json 删掉再跑）")
        on_step(0, 1.0)
        return rows

    ranges = store.to_ranges(missing)
    cached = total_days - len(missing)
    if cached:
        log(f"这段共 {total_days} 天，其中 {cached} 天存档里已有，"
            f"只需补抓 {len(missing)} 天（{len(ranges)} 段）。")
    else:
        log(f"这段 {total_days} 天存档里都没有，全抓。")

    fetched: list[dict] = []
    added_total = 0
    for i, (r1, r2) in enumerate(ranges, 1):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        log(f"\n补抓第 {i}/{len(ranges)} 段：{r1} ~ {r2}")
        part = fetch(r1, r2, log,
                     lambda _i, f, i=i: on_step(0, (i - 1 + f) / len(ranges)),
                     cancel_event)
        fetched += part
        # ⚠️ 顺序不能反：**先落盘，落盘成功了才记覆盖**。
        # 原来是每段先记覆盖、全部抓完才统一写存档 —— 第二段一炸，
        # 第一段的记录还在内存里就没了，而覆盖范围已经写下「抓过了」，
        # 下次再跑直接跳过，那几天永久丢失，而且完全无声。
        added, total = store.merge_listing(ROOT, part)
        added_total += added
        store.mark_covered(ROOT, r1, r2, key)
        log(f"  这一段新增 {added} 条，存档现有 {total} 条。")

    log(f"\n本次共新增 {added_total} 条公告进存档。")

    rows = store.listing_between(ROOT, d1, d2)
    if fetched and not rows:
        # 抓回来一堆、按日期一筛却一条不剩 —— 这在逻辑上说不通，
        # 只可能是日期解析错了。上一版就是这么无声丢掉 2594 条的：
        # 日志上写着「新增 2594 条进存档」，下一行才是「本次范围内共 0 条」。
        sample = [r.get("DATE_TIME", "") for r in fetched[:3]]
        raise DateFilterBug(
            f"抓到 {len(fetched)} 条，按 {d1} ~ {d2} 一筛却剩 0 条 —— "
            f"日期解析对不上。接口给的样子：{sample}")
    log(f"本次范围内共 {len(rows)} 条（存档 + 新抓）。")
    on_step(0, 1.0)
    return rows


def _write_listing(records: list[dict], log) -> Path:
    out_dir = ROOT / "data" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vendor_raw.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    csv_path = out_dir / "vendor_listing.csv"
    if records:
        preferred = ["NEWS_ID", "DATE_TIME", "STOCK_CODE", "STOCK_NAME",
                     "TITLE", "FILE_LINK", "FILE_INFO"]
        cols = sorted({k for r in records for k in r})
        header = [c for c in preferred if c in cols] + \
                 [c for c in cols if c not in preferred]
        with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        log(f"已保存 {csv_path.name}")
    return csv_path


def run(date_from: dt.date, date_to: dt.date, *,
        on_log=None, on_step=None, cancel_event: threading.Event | None = None,
        fetch=_fetch, open_pdf=None, force_refetch: bool = False,
        on_activity=None) -> Result:
    """跑完整条流水线。

    `fetch` 和 `open_pdf` 都可替换 —— 测试里换成假的，就不会真的联网。
    """
    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(str(text))
        if on_log:
            on_log(str(text))

    def step(index: int, frac: float = 0.0) -> None:
        if on_step:
            on_step(index, frac)

    def activity(text: str) -> None:
        """长任务的「还活着」信号。界面拿它显示当前在等谁。"""
        if on_activity:
            try:
                on_activity(text)
            except Exception:
                pass

    result = Result()
    log_path = ROOT / "run_log.txt"
    started = dt.datetime.now()
    log(f"开始 {started:%Y-%m-%d %H:%M:%S}")

    try:
        step(0)
        log(f"\n【1/5】{STEPS[0]}")
        records = _fetch_incrementally(date_from, date_to, log, step,
                                       cancel_event, fetch, force=force_refetch)
        result.fetched = len(records)
        csv_path = _write_listing(records, log)
        log(f"抓到 {len(records)} 条")

        if records:
            step(1, 0.0)
            log(f"\n【2/5】{STEPS[1]}")
            rows, rules = _screen(records, result, log)
            step(1, 1.0)

            step(2, 0.0)
            log(f"\n【3/5】{STEPS[2]}")
            result.deals = _extract_deals(rows, log, step, cancel_event,
                                          open_pdf=open_pdf,
                                          on_activity=activity)
            _write_deals(result.deals, log)
            step(2, 1.0)

            step(3, 0.0)
            log(f"\n【4/5】{STEPS[3]}")
            result.report_path = _write_report(rows, result, rules, csv_path, log)
            step(3, 1.0)
            result.ok = True
        else:
            log("\n抓到 0 条，后面几步跳过。")

        step(4, 0.0)
        log(f"\n【5/5】{STEPS[4]}")
        result.diagnostic_path = _write_diagnostic(lines, result)
        log(f"已生成 {result.diagnostic_path.name}")
        step(4, 1.0)

    except Cancelled:
        log("\n已停止。")
        result.error = "用户停止"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log("\n出错了，完整堆栈：\n" + traceback.format_exc())
        try:
            result.diagnostic_path = _write_diagnostic(lines, result)
        except Exception:
            pass

    elapsed = (dt.datetime.now() - started).total_seconds()
    log(f"\n用时 {elapsed / 60:.1f} 分钟")
    log_path.write_text("\n".join(lines), encoding="utf-8")
    result.log_path = log_path
    return result


def _screen(records, result: Result, log):
    """标题层筛选。返回 (行, 规则)。"""
    from . import screening as S

    from . import store as store_mod

    rules = S.load_rules("screening_rules.yaml")
    recs = [{
        "row_id": r.get("NEWS_ID", f"r{i}"),
        # 规范成 ISO —— 表里、答案表里、存档里全用同一种写法，
        # 否则「04/06/2026」和「2026-06-04」永远对不上。
        "date": store_mod.row_date(r),
        "code": r.get("STOCK_CODE", ""),
        "name": r.get("STOCK_NAME", ""),
        "title": r.get("TITLE", ""),
        "pdf_url": r.get("FILE_LINK", ""),
    } for i, r in enumerate(records)]

    report_obj = S.screen(recs, rules)
    result.buckets = dict(report_obj.counts)
    result.qc_notes = list(report_obj.notes)
    result.screened = len(recs)

    scr_dir = ROOT / "data" / "screening"
    scr_dir.mkdir(parents=True, exist_ok=True)
    with (scr_dir / "screened.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["row_id", "date", "code", "name", "bucket", "species",
                    "matched_exclude", "matched_retain", "manual_flags",
                    "reasons", "title", "pdf_url", "rules_version"])
        for r in recs:
            v = r["verdict"]
            w.writerow([r["row_id"], r["date"], r["code"], r["name"], v.bucket,
                        v.species, "／".join(v.matched_exclude),
                        "／".join(v.matched_retain), "；".join(v.manual_flags),
                        "；".join(v.reasons), r["title"], r["pdf_url"], rules.version])

    labels = {S.RETAINED: "留存（进抽取）", S.MANUAL: "人工复核",
              S.EXCLUDED: "已灰（后续/程序公告）", S.SPECIAL: "特殊品种",
              S.SUPERSEDED: "被取代", S.IRRELEVANT: "题材无关"}
    for bucket, n in sorted(result.buckets.items(), key=lambda kv: -kv[1]):
        log(f"  {labels.get(bucket, bucket)}: {n}")
    log(f"数量校验：{'平' if report_obj.reconciled else '不平 —— 需人工检查'}")
    for note in report_obj.notes:
        log(f"  · {note}")
    return recs, rules


def _extract_deals(rows, log, on_step, cancel_event, open_pdf=None,
                   on_activity=None) -> list[Deal]:
    """对留存桶里的公告打开 PDF，抽要约字段。

    这一步才产出你真正要的东西：要约类型、要约价、溢价率、交易规模。

    列表层改成关键词模式之后，瓶颈整个搬到了这里：一周才 4 份 PDF，
    年初至今就是一百多份。实测每份解析只要 1~2 秒，时间全花在**下载**上
    （每份 500~800 KB，从香港拉回来）—— 也就是延迟受限，不是算力受限。

    所以照搬你 asso 那份客户端对付列表的同一招：切成几段并发，
    **共用一个限速器**，让网络往返互相重叠，而对披露易的请求频率不变。
    """
    import concurrent.futures

    from . import extractor, pdf_source, selectors, validators

    on_activity = on_activity or (lambda _text: None)

    targets = [r for r in rows if r["verdict"].bucket == "retained"]
    if not targets:
        log("  留存桶为空，没有要抽的公告。")
        return []

    targets, skipped = _cap_per_target(targets)
    if skipped:
        log(f"  同一标的的后续公告先不展开，跳过 {len(skipped)} 份：")
        for code, n in sorted(skipped.items(), key=lambda kv: -kv[1])[:6]:
            log(f"      {code} 还有 {n} 份（已取最早的 {MAX_PER_TARGET} 份）")
        log(f"      —— 一单交易只有一个 T0，其余是后续公告。"
            f"改 config.yaml 的 max_per_target 可放宽。")

    _step, workers, _mode, _kw = _speed_settings()
    workers = max(1, min(workers, len(targets)))
    cache = ROOT / "data" / "cache" / "pdf"

    if open_pdf is None:
        # 三件事一起做对，缺一样都会被对方掐连接（实测 56 份挂了 3 份）：
        #   · 复用**已访问过检索页**的那个会话 —— 带着 cookie，也省掉
        #     每份都重新握手。原来每下一份就新建一个裸 session。
        #   · 所有下载共用一把限速闸 —— 并发是为了让延迟重叠，
        #     不是为了提高请求频率（asso 的 download_pdf 就漏了这一步）。
        #   · 失败退避重试 —— 偶发的 ConnectionReset 不该让那一单永久丢数据。
        import sys
        sys.path.insert(0, str(ROOT / "vendor"))
        import config as vendor_config          # noqa: E402
        from hkex_client import HKEXClient      # noqa: E402

        client = HKEXClient()
        limiter = pdf_source.RateLimiter(vendor_config.SLEEP_BETWEEN_REQUESTS)

        keep = _keep_raw_files()
        if not keep:
            log("  （config.yaml 里 keep_raw_files=false：原件用完即弃，不留副本）")

        def opener(url):
            def note_retry(attempt, total, exc):
                log(f"      下载失败（第 {attempt}/{total} 次），稍后重试："
                    f"{type(exc).__name__}")
                on_activity(f"重试第 {attempt}/{total} 次…")

            data, cached = pdf_source.fetch_bytes(
                url, cache, session=client.session, limiter=limiter,
                on_retry=note_retry)
            if not keep:
                # 用完即弃 —— asso 那份客户端的 download_pdf 就是这么做的
                # （`return r.content`，从不落盘）。省地方，但审计追溯断了。
                pdf_source.discard_cached(url, cache)
            # ⚠️ 解析放在**调用方**串行做，不在这里 —— 见 parse_lock 的注释
            return _Fetched(url, data, cached)
    else:
        opener = open_pdf

    # 存档里已经抽过、而且抽取器版本一致的，直接复用 —— PDF 都不用打开。
    # 版本对不上就重抽：我改了正则却拿旧结果冒充新结果，表面一切正常，
    # 数字却是旧逻辑抽的，那正是铁律二说的静默污染。
    from . import store
    archived = store.load_deals(ROOT)
    reused_rows, todo = [], []
    for row in targets:
        old_row = archived.get(row["row_id"])
        if old_row and store.reusable(old_row):
            reused_rows.append(old_row)
        else:
            todo.append(row)

    if reused_rows:
        log(f"  留存桶 {len(targets)} 条，其中 {len(reused_rows)} 条存档里已抽过"
            f"（版本 {store.EXTRACTOR_VERSION}），直接复用。")
    targets = todo
    if not targets:
        log("  没有需要新抽的公告，一份 PDF 都不用下。")
    else:
        log(f"  需要新抽 {len(targets)} 条，{workers} 路并发打开 PDF"
            f"（共用会话与限速闸，失败自动重试）…")

    done = [0]
    lock = threading.Lock()
    busy: dict = {}          # 谁 → 什么时候开始的

    def _announce() -> None:
        """把「正在处理哪几份、各等了多久」告诉界面。

        「85/86 之后一直等」的真相是：并发跑批时，**最慢的那一份必然排在
        最后**。85 份早就完事了，你盯着的是剩下那一份在死链上重试。
        这不是最后一份特别慢，是慢的那份定义上就是最后一份。

        既然消不掉，就让它可预期：显示还剩几份、分别是谁、各等了多久、
        最多还要等多久。有边界的等待和卡死是两回事。
        """
        now = time.monotonic()
        with lock:
            items = sorted(busy.items(), key=lambda kv: kv[1])[:3]
        if not items:
            on_activity(f"{done[0]}/{len(targets)} 份")
            return
        parts = [f"{who}（等了 {int(now - t0)} 秒）" for who, t0 in items]
        left = len(targets) - done[0]
        tail = f"　最多再等 {MAX_SECONDS_PER_PDF} 秒" if left <= 3 else ""
        on_activity(f"{done[0]}/{len(targets)} 份　还剩 {left} 份："
                    + "、".join(parts) + tail)

    # 解析不再串行了 —— 因为它已经不在这个进程里跑。
    #
    # 早先测出「4 路并发解析比 1 路慢 70%」（12.13 秒 → 20.56 秒），
    # 据此加了一把全程持有的解析锁。那个结论**只在线程里成立**：
    # CPU 型工作在一个解释器里受 GIL 限制没法真并行，多开线程只是把
    # 同样的活切碎轮流做。
    #
    # 解析搬进子进程之后没有 GIL 了，重测（8 份 60 页）：
    #   1 个进程 17.3 秒 / 2 个 8.8 秒 / 3 个 6.6 秒 / 4 个 4.7 秒
    # 3.7 倍。同一个结论，换了执行模型就正好反过来 ——
    # 所以那把锁撤掉，改由 ParsePool 的槽位数控制并行度。

    # 解析要在**另一个进程**里跑。
    #
    # 一次实跑「一个小时没动」：日志停在 27/68，界面标题「未响应」，
    # 界面上「已运行」的秒数冻在 8 分 38 秒 —— 秒数冻住本身就是证据，
    # 主线程被一起拖死了，不是慢，是停。
    #
    # MAX_SECONDS_PER_PDF 那 90 秒从来没被执行过，它只被拿去拼了一句
    # 「最多再等 90 秒」给用户看。Python 没有办法中断一个线程：
    # pdfplumber 一旦在某份文件上陷进去，那个线程永远回不来，
    # 而它还攥着上面那把解析锁，后面 41 份全部堵死。
    #
    # 线程杀不掉，进程杀得掉。
    # 没有要新抽的就别起 —— 存档全命中时一份 PDF 都不用开，
    # 为了零份公告 spawn 一个进程纯属浪费。
    from .parsepool import ParsePool
    pool = ParsePool(timeout=_parse_timeout(), workers=_parse_workers(),
                     on_note=lambda t: log(f"  【注意】{t}")) if targets else None

    def one(row) -> Deal:
        # 增删和读取必须同一把锁 —— 一个线程在 sorted(busy) 的同时
        # 另一个线程 add，会抛 RuntimeError: Set changed size during iteration，
        # 而那是在 4 路并发里偶发的，最难复现的那种。
        who = row.get("code") or row.get("row_id")
        started = time.monotonic()
        with lock:
            busy[who] = started
        _announce()
        try:
            deal = _extract_one(row, opener, cancel_event, extractor, pdf_source,
                                selectors, validators, None, pool)
        finally:
            with lock:
                busy.pop(who, None)
            _announce()
        with lock:
            done[0] += 1
            n = done[0]
        on_step(2, n / len(targets))
        # 每份花了多久、翻了几页 —— 慢在哪里必须能从日志上直接读出来，
        # 而不是靠猜。上一轮「20 分钟卡在 71」就是猜了两次才找对方向。
        cost = f"{time.monotonic() - started:.0f}秒{deal.pages_note}"
        if deal.verdict == "offer":
            log(f"    [{n}/{len(targets)}] [OK] {deal.code} {deal.name}　"
                f"← {deal.offeror or '要约方未识别'}　"
                f"{deal.offer_type}　{deal.offer_price}　"
                f"{deal.premium_pct}%　{deal.deal_size}　{cost}")
        else:
            log(f"    [{n}/{len(targets)}] [--] {deal.code} {deal.name}　"
                f"{VERDICT_LABEL.get(deal.verdict, '')}：{deal.verdict_reason}"
                f"　{cost}")
        return deal

    # 每完成这么多份就往存档里刷一次。
    # 原来是全部跑完才存 —— 卡在最后一份时按「停止」，前面 85 份的
    # 抽取成果全部白费，下次还得从头再下一遍。
    FLUSH_EVERY = 10
    deals: list[Deal] = []

    def flush(rows: list[Deal]) -> None:
        keep_rows = [{"NEWS_ID": d.news_id, "抽取器版本": store.EXTRACTOR_VERSION,
                      **dict(zip(DEAL_COLUMNS, _deal_row(d)))}
                     for d in rows if d.news_id]
        if keep_rows:
            store.merge_deals(ROOT, keep_rows, DEAL_COLUMNS)
            store.merge_evidence(ROOT, {d.pdf_url: d.evidence
                                        for d in rows if d.pdf_url and d.evidence})

    try:
        if workers == 1:
            for row in targets:
                deals.append(one(row))
                if len(deals) % FLUSH_EVERY == 0:
                    flush(deals[-FLUSH_EVERY:])
        else:
            # 这里的 threads 是**下载**的并发；解析归上面那个子进程。
            # 原来这个变量也叫 pool，两个 pool 撞在一起是最不该有的那种 bug。
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as threads:
                futures = [threads.submit(one, r) for r in targets]
                pending = []
                for fut in futures:        # 按提交顺序收，输出稳定可复现
                    deal = fut.result()
                    deals.append(deal)
                    pending.append(deal)
                    if len(pending) >= FLUSH_EVERY:
                        flush(pending)
                        pending = []
                flush(pending)
    except (Cancelled, KeyboardInterrupt):
        # 停在半路也要把已经抽好的存下来 —— 下次接着跑，不用重下
        flush(deals)
        log(f"  已停止，但前 {len(deals)} 份的抽取结果已存档，下次不用重抽。")
        raise
    finally:
        # 解析子进程一定要收掉。留一个孤儿进程在后台啃 PDF，
        # 用户关了窗口还听见风扇响 —— 那是最招人烦的那种 bug。
        if pool is not None:
            pool.close()

    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()

    # ⚠️ 三步的顺序是有讲究的：先合并，再标镜像，最后才存档。
    #
    # 原来是「先存档、再标镜像」，于是存档里永远记的是**没标过**的结果；
    # 加上「全部可复用就早退」那条路径跳过了标记，同一份数据第一遍记
    # 1 单、第二遍记 2 单 —— 做中位数时这一单的权重凭空翻倍，
    # 而两次运行的日志都显示「成功」。
    fresh_ids = {d.news_id for d in deals if d.news_id}
    deals = [_deal_from_row(r) for r in reused_rows] + deals

    mirrors = mark_mirror_filings(deals)
    if mirrors:
        log(f"  发现 {mirrors} 条镜像归档（要约方自己那一边），已标出不重复计数")

    dupes = mark_duplicate_filings(deals)
    if dupes:
        log(f"  发现 {dupes} 条同单重复（先公告、后综合文件），已合并记一单")

    # 存的是标记之后的结果。镜像那一行也要更新回存档，
    # 否则下次复用出来又是重复的。
    to_save = [d for d in deals
               if d.news_id and (d.news_id in fresh_ids
                                 or d.verdict in ("mirror", "duplicate"))]
    if to_save:
        rows = [{"NEWS_ID": d.news_id, "抽取器版本": store.EXTRACTOR_VERSION,
                 **dict(zip(DEAL_COLUMNS, _deal_row(d)))} for d in to_save]
        total = store.merge_deals(ROOT, rows, DEAL_COLUMNS)
        store.merge_evidence(ROOT, {d.pdf_url: d.evidence
                                    for d in to_save if d.pdf_url and d.evidence})
        log(f"  已存档 {len(rows)} 条抽取结果，存档现有 {total} 条。")

    n, size = cache_info()
    if n:
        log(f"  公告原件副本：{n} 份，占 {human_size(size)}"
            f"（{CACHE_DIR}，用于幂等重跑与审计追溯）")
    return deals


# 列名 → Deal 上的字段名。溢价梯子那几列不在这里 ——
# 它们由 premium_ladder 展开，单独还原。
#
# 【注意】这张表和 _deal_row 必须一一对应，任何一边加了列而另一边忘了，
# 存档读回来就会静默丢字段。test_a_deal_survives_a_round_trip_through_the_store
# 拿一个字段全填满的 Deal 走一遍存盘再读回，逐字段比对，专门守这个。
_FROM_ROW = {
    "判定理由": "verdict_reason",
    "交易性质(待确认)": "nature", "性质依据": "nature_reasons",
    "公告日期": "date", "股票代码": "code", "板块": "board",
    "受要约方": "name", "受要约方全称": "target_full",
    "要约方": "offeror", "要约方财务顾问": "offeror_fa",
    "要约类型": "offer_type", "条件": "is_conditional",
    "对价形式": "consideration",
    "要约价(HKD)": "offer_price", "主值溢价率(%)": "premium_pct",
    "主值口径": "premium_basis",
    "六个月最低": "six_month_low", "六个月最高": "six_month_high",
    "泄露涨幅(%)": "runup_pct",
    "每股NAV": "nav_per_share", "市净率P/B": "pb_ratio",
    "隐含股权价值(HKD)": "implied_equity_value", "已发行股数": "total_shares",
    "交易规模(HKD)": "deal_size", "上市地位意向": "listing_intent",
    "停牌前最后交易日": "last_trading_day",
    "置信度": "confidence", "复算校验": "checks", "备注": "notes",
    "公告标题": "title", "PDF链接": "pdf_url",
}

_LABEL_TO_VERDICT = {v: k for k, v in VERDICT_LABEL.items()}


def _deal_from_row(row: dict) -> Deal:
    """存档里的一行 → Deal。存档复用这条路，所以它必须是无损的。"""
    deal = Deal(news_id=str(row.get("NEWS_ID", "")))
    for col, attr in _FROM_ROW.items():
        if col in row:
            setattr(deal, attr, row[col] or "")
    deal.verdict = _LABEL_TO_VERDICT.get(row.get("判定", ""), "unclear")
    deal.premium_ladder = {
        col[1:-len("(%)")]: row[col] for col in row
        if col.startswith("较") and col.endswith("(%)") and str(row[col]).strip()}
    return deal


class _Fetched:
    """刚下回来、还没解析的公告字节。"""

    __slots__ = ("url", "data", "from_cache")

    def __init__(self, url, data, from_cache):
        self.url, self.data, self.from_cache = url, data, from_cache


def _extract_one(row, opener, cancel_event, extractor, pdf_source,
                 selectors, validators, parse_lock=None, pool=None) -> Deal:
    """抽一份公告。抽挂了变成 Deal 上的一条备注，不能连累其余几百份。"""
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()

    # 板块从文件路径就能读出来：/sehk/ 是主板，/gem/ 是创业板。
    # GEM 单可比性弱，做可比表时要能一眼分出来。
    link = row["pdf_url"] or ""
    board = "GEM" if "/gem/" in link.lower() else (
        "主板" if "/sehk/" in link.lower() else "")
    deal = Deal(news_id=row.get("row_id", ""),
                code=row["code"], name=row["name"], date=row["date"],
                board=board,
                title=row["title"], pdf_url=pdf_source.full_url(row["pdf_url"]))
    if True:
        try:
            got = opener(deal.pdf_url)
            if isinstance(got, _Fetched):
                # 下载完了才排队解析 —— 排队的是 CPU，不是网络
                # 解析放在子进程里，而且外层仍然串行 —— 见 parse_lock 的注释。
                # 先翻前几页问一句「这文件值得翻完吗」，不值得就不翻。
                probe = _probe_pages()
                if parse_lock is not None:
                    with parse_lock:
                        doc = pool.parse(got.url, got.data, probe_pages=probe)
                else:
                    doc = pool.parse(got.url, got.data, probe_pages=probe)
                doc.from_cache = got.from_cache
            else:
                doc = got                      # 测试注入的假 PDF
            if not doc.has_text_layer:
                deal.notes = "扫描件无文本层，需 OCR 并人工复核"
                deal.confidence = "low"
                deal.verdict, deal.verdict_reason = "unclear", "扫描件无文本层"
                return deal

            # 全用 getattr：这行只是日志上的一句话，绝不能因为拿不到
            # 某个属性就把整份公告的抽取搞挂。
            parsed = getattr(doc, "pages_parsed", 0) or len(doc.pages)
            total_pages = getattr(doc, "page_count", 0) or parsed
            deal.pages_note = (f"／{parsed}页" if parsed >= total_pages
                               else f"／{parsed}页(共{total_pages})")

            ex = extractor.extract(row["title"], doc.pages)
            if getattr(doc, "stopped_early", False):
                ex.notes.append(
                    f"只解析了前 {doc.pages_parsed} 页（全文 {doc.page_count} 页）："
                    f"前几页没有任何要约迹象")
            deal.offeror = ex.offeror
            deal.offeror_fa = ex.offeror_fa
            # 标题没写受要约方全称时退回披露易给的简称 —— 那是它自己的归属，
            # 比从正文里猜可靠
            deal.target_full = ex.target or row["name"]
            deal.last_trading_day = ex.last_trading_day
            deal.offer_type = ex.offer_type
            deal.obligation_basis = ex.obligation_basis
            deal.offer_scope = ex.offer_scope
            deal.consideration = ex.consideration
            deal.offer_price = ex.offer_price
            deal.price_headline = ex.price_headline
            deal.deal_size = ex.deal_size
            deal.listing_intent = ex.listing_intent
            deal.confidence = ex.confidence
            deal.verdict, deal.verdict_reason = ex.verdict()
            deal.notes = "；".join(ex.notes)

            comps = [{"anchor": c.anchor, "window": c.window, "label": c.label,
                      "stated_pct": c.stated_pct,
                      "stated_direction": c.stated_direction,
                      "benchmark": c.benchmark,
                      "page": c.page, "quote": c.quote} for c in ex.comparisons]
            rules = _premium_rules()
            pick = selectors.select_primary_premium(
                comps, window=rules["window"],
                anchor_priority=rules["anchor_priority"],
                fallback=rules["fallback"])
            if pick:
                deal.premium_pct = str(pick.signed_pct)
                deal.premium_basis = pick.label
                _reconcile_premium_direction(deal, pick, ex)
                _note_other_anchor(deal, pick)

            # 整条溢价梯子都留着：投行看可比不会只看一个口径，
            # 而且下一个人可能要按「最后交易日收市价」重排
            deal.premium_ladder = {
                c.label: ("-" if c.stated_direction == "discount" else "")
                         + c.stated_pct for c in ex.comparisons}

            # 估值组：算术全在 Python（铁律一），每个结果都说得出依据
            from . import valuation
            derived = valuation.derive(
                offer_price=ex.offer_price, total_shares=ex.total_shares,
                nav_per_share=ex.nav_per_share,
                undisturbed_spot=ex.spot("undisturbed"),
                last_trading_spot=ex.spot("last_trading_day"))
            deal.total_shares = ex.total_shares
            deal.nav_per_share = ex.nav_per_share
            deal.implied_equity_value = derived.implied_equity_value
            deal.pb_ratio = derived.pb_ratio
            deal.runup_pct = derived.runup_pct
            deal.six_month_low = ex.six_month_low
            deal.six_month_high = ex.six_month_high
            deal.is_conditional = ex.is_conditional

            # 交易性质只是建议值 —— 真收购和买壳混在一起算中位数就是废数据，
            # 但这条属于分类层（铁律二），错了是静默污染，所以永远带「待确认」
            guess = valuation.guess_nature(
                premium_pct=deal.premium_pct, listing_intent=ex.listing_intent,
                debt_conversion=ex.debt_conversion, offer_type=ex.offer_type,
                runup_pct=derived.runup_pct)
            deal.nature = guess.label
            deal.nature_reasons = "；".join(guess.reasons)

            deal.checks = _run_checks(ex, validators, deal.premium_pct)
            deal.evidence = {
                "当事方": [ex.parties_evidence.page, ex.parties_evidence.quote],
                "要约类型": [ex.offer_type_evidence.page, ex.offer_type_evidence.quote],
                "要约价": [ex.offer_price_evidence.page, ex.offer_price_evidence.quote],
                "交易规模": [ex.deal_size_evidence.page, ex.deal_size_evidence.quote],
                "溢价率": [pick.page, pick.source_quote] if pick else [0, ""],
                "上市意向": [ex.listing_intent_evidence.page,
                             ex.listing_intent_evidence.quote],
                "已发行股数": [ex.total_shares_evidence.page,
                               ex.total_shares_evidence.quote],
                "条件": [ex.is_conditional_evidence.page,
                         ex.is_conditional_evidence.quote],
                "债转股痕迹": [ex.debt_conversion_evidence.page,
                               ex.debt_conversion_evidence.quote],
                "隐含股权价值": [0, derived.implied_equity_basis],
                "市净率": [0, derived.pb_basis],
                "泄露涨幅": [0, derived.runup_basis],
            }
        except Cancelled:
            raise
        except Exception as exc:
            # 解析超时单独说 —— 它和「链接坏了」「不是 PDF」是完全不同的
            # 一件事：文件好好的，是这份太难啃。人看到这行才知道该去
            # 手工打开它，而不是以为下载失败了。
            from .parsepool import ParseTimeout
            if isinstance(exc, ParseTimeout):
                deal.notes = f"解析超时：{exc}"
                deal.confidence = "low"
                deal.verdict = "unclear"
                deal.verdict_reason = (
                    f"解析超时：{exc}。这份公告太难解析，已跳过，请手工打开原文；"
                    f"想给它更多时间就调 config.yaml 的 pdf.parse_timeout_seconds")
            else:
                deal.notes = f"抽取失败：{type(exc).__name__}: {exc}"
                deal.confidence = "low"
                deal.verdict, deal.verdict_reason = "unclear", f"抽取失败：{exc}"
    return deal


def _note_other_anchor(deal, pick) -> None:
    """同一个窗口下还有另一个锚点时，把它也写进备注。

    这个选择是有争议的，而且争议来自你自己的两单答案：
        3336 巨騰   你取 -15.45%（未受干扰日前30日），不是 -17.50%（最后交易日）
        08439 新百利 你取 +40.80%（最后交易日前30日），不是 +129.8%（未受干扰日）
    两单都是「两个锚点都有」，选法相反 —— 一条规则解释不了两单。
    所以程序按 config.yaml 里的优先级选一个，**同时把另一个摆出来**：
    要改哪一单，看一眼备注就能改，不用回去翻原文。
    """
    if not pick.other_anchor or not pick.other_pct:
        return
    deal.notes = "；".join(x for x in (
        deal.notes,
        f"同窗口另一口径：较{pick.other_anchor} {pick.other_pct}%"
        f"（本表按 {pick.label} 取值）") if x)


def _reconcile_premium_direction(deal, pick, ex) -> None:
    """主值溢价率的符号，跟公告自己的两个数字对一遍（铁律一）。

    符号错比数值错严重：折让被写成溢价，那一行放进可比表会得出
    完全相反的结论，而光看百分比看不出来 —— 必须比大小。

    两种处理，分得很清楚：
      · 百分比复算得上 → 数字自洽，错的只是「溢价／折让」那两个字，
        按算术改符号。这不是判断，是公告自己的两个数除出来的。
      · 百分比复算不上 → 我多半把基准价配错了行（PDF 表格错位那类），
        两边都不可信。**不改数**，只把口径标上问号、置信度降到低，
        让人一眼看得见（铁律二：宁可标出来，不要静默改）。
    """
    from . import selectors

    check = selectors.check_direction(pick, ex.offer_price)
    if check is None or check.agrees:
        return

    if check.pct_agrees:
        from decimal import Decimal
        deal.premium_pct = str(-Decimal(deal.premium_pct))
        note = f"溢价方向按公告自己的数字纠正：{check.detail}"
    else:
        deal.premium_basis = f"{pick.label}（方向存疑）"
        deal.confidence = "low"
        note = f"溢价方向与基准价对不上，数值未改，请人工核：{check.detail}"
    deal.notes = "；".join(x for x in (deal.notes, note) if x)


def _run_checks(ex, validators, premium_pct: str = "") -> str:
    """跑 V4/V5/V6/V15，把结果压成一行。铁律一：算术全在这里，不在抽取层。"""
    from decimal import Decimal, InvalidOperation

    floor: list = []
    if premium_pct:
        try:
            floor = [validators.v15_discount_floor("主值溢价率",
                                                   Decimal(str(premium_pct)))]
        except InvalidOperation:
            floor = []
    if not ex.comparisons or not ex.offer_price:
        bad = [f"{f.code}:{f.subject}" for f in floor if not f.passed]
        return "未通过 " + "；".join(bad) if bad else ""
    offer = Decimal(ex.offer_price)
    comparisons = [validators.PriceComparison(
        label=c.label, benchmark=Decimal(c.benchmark),
        benchmark_decimals=c.benchmark_decimals,
        benchmark_is_exact=c.benchmark_is_exact,
        stated_pct=Decimal(c.stated_pct), stated_direction=c.stated_direction,
        page=c.page, source_quote=c.quote) for c in ex.comparisons]

    low = Decimal(ex.six_month_low) if ex.six_month_low else Decimal(0)
    high = Decimal(ex.six_month_high) if ex.six_month_high else Decimal("9" * 12)
    nonmarket = frozenset(c.label for c in ex.comparisons if c.anchor == "nav")
    findings = validators.run_price_comparisons(offer, comparisons, low, high,
                                                nonmarket_labels=nonmarket) + floor
    failed = [f"{f.code}:{f.subject}" for f in findings if not f.passed]
    return "全部通过" if not failed else "未通过 " + "；".join(failed[:3])


# 溢价梯子的固定列。做可比表时人人都要按同一口径横向对齐，
# 所以列是固定的：某一单没有这个口径就留空，绝不用别的口径顶上。
LADDER_COLUMNS = [
    "最后交易日收市价", "最后交易日前5日均价", "最后交易日前10日均价",
    "最后交易日前30日均价", "最后交易日前60日均价", "最后交易日前180日均价",
    "未受干扰日收市价", "未受干扰日前5日均价", "未受干扰日前10日均价",
    "未受干扰日前30日均价", "未受干扰日前60日均价", "未受干扰日前180日均价",
    "3.7公告前收市价",
    "每股净资产",
]

# 兜底列：公告用了固定列以外的口径时，原样写在这里。
# 没有这一列，那几条比较就悄悄消失了 —— 1417 的「規則3.7 公告前」
# 和 3336 的「前180日均价」当初就是这么丢的。
LADDER_OTHER = "其他比较项"

DEAL_COLUMNS = [
    # 第一层：识别与筛选（决定这单能不能进样本）
    "判定", "判定理由", "交易性质(待确认)", "性质依据",
    "公告日期", "股票代码", "板块", "受要约方", "受要约方全称",
    "要约方", "要约方财务顾问",
    "要约类型", "义务基础", "要约范围", "条件", "对价形式",
    # 第二层：定价
    "要约价(HKD)", "另一套要约价", "主值溢价率(%)", "主值口径",
    *[f"较{c}(%)" for c in LADDER_COLUMNS], LADDER_OTHER,
    "六个月最低", "六个月最高", "泄露涨幅(%)",
    # 估值组 —— 和规模分开：付给公众股东的才是规模
    "每股NAV", "市净率P/B", "隐含股权价值(HKD)", "已发行股数",
    # 第四层：规模与执行
    "交易规模(HKD)", "上市地位意向", "停牌前最后交易日",
    "置信度", "复算校验", "备注", "公告标题", "PDF链接",
]


def _deal_row(d: Deal) -> list[str]:
    other = "；".join(f"较{k} {v}%" for k, v in d.premium_ladder.items()
                      if k not in LADDER_COLUMNS)
    return [VERDICT_LABEL.get(d.verdict, d.verdict), d.verdict_reason,
            d.nature, d.nature_reasons,
            d.date, d.code, d.board, d.name, d.target_full,
            d.offeror, d.offeror_fa,
            d.offer_type, OBLIGATION_LABEL.get(d.obligation_basis, ""),
            SCOPE_LABEL.get(d.offer_scope, ""),
            d.is_conditional, d.consideration,
            d.offer_price, d.price_headline, d.premium_pct, d.premium_basis,
            *[d.premium_ladder.get(c, "") for c in LADDER_COLUMNS], other,
            d.six_month_low, d.six_month_high, d.runup_pct,
            d.nav_per_share, d.pb_ratio, d.implied_equity_value, d.total_shares,
            d.deal_size, d.listing_intent, d.last_trading_day,
            d.confidence, d.checks, d.notes, d.title, d.pdf_url]


# Excel 打开 CSV 会把 01417 当数字吞成 1417（你自己踩过：代码列设文本防吞零）。
# 写成 ="01417" 是唯一在 Excel / WPS / LibreOffice 里都保住前导零的写法，
# 读回来时 load_rows 再脱掉这层壳，两边都不别扭。
_TEXT_COLUMNS = {"股票代码"}


def _excel_text(value: str) -> str:
    return f'="{value}"' if value else value


def _write_deals(deals: list[Deal], log) -> None:
    if not deals:
        return
    out = ROOT / "data" / "deals.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    text_at = [i for i, c in enumerate(DEAL_COLUMNS) if c in _TEXT_COLUMNS]
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(DEAL_COLUMNS)
        for d in deals:
            row = _deal_row(d)
            for i in text_at:
                row[i] = _excel_text(row[i])
            w.writerow(row)

    # 出处单独存：CSV 塞不下整段引文，而重启后没有出处就违背铁律三
    from . import dealsview
    dealsview.save_evidence(deals, ROOT / "data" / "deals_evidence.json")
    log(f"  已保存 {out.name}（{len(deals)} 单）")


def _write_report(rows, result: Result, rules, csv_path, log) -> Path:
    from . import report as R

    scr_dir = ROOT / "data" / "screening"
    with (scr_dir / "screened.csv").open(encoding="utf-8-sig", newline="") as fh:
        table = list(csv.DictReader(fh))
    manual = sum(1 for r in table if r.get("bucket") == "manual")
    notes = [f"人工复核桶有 {manual} 条，须逐条看完（铁律二）。"] if manual else []
    notes += list(result.qc_notes)
    path = R.write_report(table, scr_dir / "report.html",
                          rules_version=rules.version, source=str(csv_path),
                          notes=notes, deals=result.deals)
    log(f"已生成 {path.name}")
    return path


def _write_diagnostic(lines: list[str], result: Result) -> Path:
    """把这次运行压成一个文件，发给 Claude 就够了。"""
    import platform
    import sys

    out = ROOT / "SEND_TO_CLAUDE.txt"
    parts = [
        "=" * 64, "环境", "=" * 64,
        f"系统：{platform.platform()}",
        f"Python：{sys.version}",
        f"目录：{ROOT}",
        "", "=" * 64, "结果", "=" * 64,
        f"抓到：{result.fetched} 条",
        f"筛查：{result.screened} 条",
        f"判定桶：{result.buckets}",
        f"错误：{result.error or '（无）'}",
        "", "=" * 64, "运行日志", "=" * 64,
        *lines[-400:],
    ]
    out.write_text("\n".join(str(p) for p in parts), encoding="utf-8")
    return out


# ---------------------------------------------------------------- 原文摘录

# 摘录时围绕这些词各取前后一段。它们是抽取层真正要找的锚点 ——
# 摘出来的就是「正则本该命中却没命中」的那几句。
_WORDING_ANCHORS = [
    "價值比較", "价值比较", "要約價", "註銷價", "收購價",
    "溢價", "折讓", "最高現金", "代價總額", "總代價", "現金代價",
    "最後交易日", "未受干擾",
]
_WORDING_WINDOW = 450       # 每个锚点前后各取多少字


def wording_excerpt(pages: dict, anchors=None, window: int = _WORDING_WINDOW,
                    limit: int = 12) -> str:
    """从解析出来的正文里，摘出锚点附近那几段。

    为什么不是整篇导出：一份公告两三万字，贴过来没人看得完，而真正
    有用的就是「較最後交易日收市價每股 X 港元折讓約 Y%」那几句。
    摘录只取锚点附近，既够我改正则，也不会把整份文件搬来搬去。
    """
    from .extractor import _flat

    anchors = anchors or _WORDING_ANCHORS
    out, taken = [], 0
    for page in sorted(pages):
        flat = _flat(pages[page])
        spans: list[tuple[int, int]] = []
        for word in anchors:
            start = 0
            while True:
                i = flat.find(word, start)
                if i < 0:
                    break
                spans.append((max(0, i - window), min(len(flat), i + window)))
                start = i + len(word)
        if not spans:
            continue
        # 相邻的窗口合并，免得同一段被抄好几遍
        spans.sort()
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in merged:
            if taken >= limit:
                out.append("…（后面还有，已截断）")
                return "\n\n".join(out)
            out.append(f"—— 第 {page} 页 ——\n{flat[a:b].strip()}")
            taken += 1
    return "\n\n".join(out) if out else "（这份公告里一个锚点都没出现）"


def export_wording(rows: list[dict], on_log=None) -> Path:
    """把几单公告的关键段落摘出来写成一个文件，好发给我改正则。

    这个功能存在的理由，是我手上真实公告正文只有 1,805 字（1417 /
    3336 / 00195 三份的价值比较节），而你一次实跑要读一百多万字。
    54 条正则全是从那 1,805 字 + 每次实跑暴露的失败反推出来的 ——
    覆盖不到的措辞就抽不出来，溢价率那 9 个空白全是这么来的。

    靠人工从 PDF 里复制原文太慢，所以让程序自己摘：给它几行结果，
    它把对应公告重新取回来、解析、摘出锚点附近那几段。
    """
    def log(text: str = "") -> None:
        if on_log:
            on_log(str(text))

    from . import extractor, pdf_source
    from .parsepool import ParsePool

    cache = ROOT / "data" / "cache" / "pdf"
    out = ROOT / "原文摘录.txt"
    parts = [
        "这个文件是给 Claude 改正则用的：只摘了公告里带「價值比較 / 要約價 /",
        "溢價 / 折讓 / 代價」这些词的段落，不是全文。",
        "", "=" * 64, ""]

    pool = ParsePool(timeout=_parse_timeout(), on_note=log)
    try:
        for row in rows:
            code = row.get("股票代码", "") or "（无代码）"
            name = row.get("受要约方", "")
            url = row.get("PDF链接", "")
            head = f"{code} {name}　{row.get('公告日期', '')}"
            log(f"  正在取 {head} …")
            if not url:
                parts += [head, "（这一行没有 PDF 链接，跳过）", "", "-" * 64, ""]
                continue
            try:
                data, _ = pdf_source.fetch_bytes(url, cache)
                doc = pool.parse(url, data, probe_pages=0)
                body = wording_excerpt(doc.pages)
            except Exception as exc:                      # noqa: BLE001
                body = f"（取不回来：{type(exc).__name__}: {exc}）"
            parts += [head, url, "", body, "", "-" * 64, ""]
    finally:
        pool.close()

    out.write_text("\n".join(parts), encoding="utf-8")
    log(f"已写出 {out}")
    return out

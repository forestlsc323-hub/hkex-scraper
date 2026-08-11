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


VERDICT_LABEL = {"offer": "要约", "unclear": "待核", "not_offer": "非要约",
                 "mirror": "镜像重复"}

# 一份公告最多占用多久。3 次尝试 × 25 秒读超时 + 退避 ≈ 80 秒，
# 这个数字是给用户看的上界：等待有边界，就不是卡死。
MAX_SECONDS_PER_PDF = 80


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
        groups.setdefault((d.date, _normalise(d.title)), []).append(d)

    marked = 0
    for group in groups.values():
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
                    f"与同日同标题的另一条重复；本行的公司「{d.name}」就是要约方，"
                    f"受要约方是同组另一条。合并记一单（坑⑨）")
                marked += 1
    return marked


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
    consideration: str = ""           # 现金 / 证券 / 现金＋证券
    offer_price: str = ""
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

    for c1, c2 in chunks:
        for kw in keywords:
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled()
            params = {
                "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
                "market": "SEHK", "stockId": "-1", "documentType": "-1",
                "fromDate": c1.strftime("%Y%m%d"), "toDate": c2.strftime("%Y%m%d"),
                "title": kw, "searchType": "0",
                "t1code": "-2", "t2Gcode": "-2", "t2code": "-2",
                "rowRange": str(vendor_config.ROW_RANGE_STEP), "lang": "zh",
            }
            resp = client.session.get(vendor_config.HKEX_SEARCH_URL,
                                      params=params,
                                      timeout=vendor_config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            raw = payload.get("result")
            records = _json.loads(raw) if raw not in (None, "null") else []
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
            time.sleep(vendor_config.SLEEP_BETWEEN_REQUESTS)

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


def clear_cache() -> tuple[int, int]:
    """删掉公告原件副本，腾地方。返回 (删了几份, 腾出多少字节)。

    删了不影响已经抽出来的 deals.csv —— 但重跑时要重新下载，
    而且如果哪份公告已经被披露易换掉，那一单就再也复现不了原样。
    """
    count, size = cache_info()
    path = ROOT / CACHE_DIR
    if path.exists():
        for f in path.iterdir():
            if f.is_file():
                f.unlink()
    return count, size


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

    # 解析全程持有这把锁 —— 也就是说**同一时刻只有一份公告在解析**。
    #
    # 这不是保守，是实测出来的：12 份真实公告只算解析，
    #   1 路 12.13 秒 / 2 路 13.59 秒 / 4 路 20.56 秒
    # 解析是 CPU 型工作，GIL 让它没法真并行，多开线程只是把同样的活
    # 切碎轮流做 —— 总时间反而涨 70%，还把 Tk 主线程一起拖住
    # （界面卡顿从 74 毫秒涨到 328 毫秒，这就是「未响应」的来源）。
    #
    # 下载是 I/O 型工作，等网络时会释放 GIL，那才是并发真正能赚到的地方。
    # 所以：下载并发、解析串行。
    parse_lock = threading.Lock()

    def one(row) -> Deal:
        # 增删和读取必须同一把锁 —— 一个线程在 sorted(busy) 的同时
        # 另一个线程 add，会抛 RuntimeError: Set changed size during iteration，
        # 而那是在 4 路并发里偶发的，最难复现的那种。
        who = row.get("code") or row.get("row_id")
        with lock:
            busy[who] = time.monotonic()
        _announce()
        try:
            deal = _extract_one(row, opener, cancel_event, extractor, pdf_source,
                                selectors, validators, parse_lock)
        finally:
            with lock:
                busy.pop(who, None)
            _announce()
        with lock:
            done[0] += 1
            n = done[0]
        on_step(2, n / len(targets))
        if deal.verdict == "offer":
            log(f"    [{n}/{len(targets)}] [OK] {deal.code} {deal.name}　"
                f"← {deal.offeror or '要约方未识别'}　"
                f"{deal.offer_type}　{deal.offer_price}　"
                f"{deal.premium_pct}%　{deal.deal_size}")
        else:
            log(f"    [{n}/{len(targets)}] [--] {deal.code} {deal.name}　"
                f"{VERDICT_LABEL.get(deal.verdict, '')}：{deal.verdict_reason}")
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
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(one, r) for r in targets]
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

    # 存的是标记之后的结果。镜像那一行也要更新回存档，
    # 否则下次复用出来又是重复的。
    to_save = [d for d in deals
               if d.news_id and (d.news_id in fresh_ids or d.verdict == "mirror")]
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
                 selectors, validators, parse_lock=None) -> Deal:
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
                if parse_lock is not None:
                    with parse_lock:
                        doc = pdf_source.parse_doc(got.url, got.data,
                                                   from_cache=got.from_cache)
                else:
                    doc = pdf_source.parse_doc(got.url, got.data,
                                               from_cache=got.from_cache)
            else:
                doc = got                      # 测试注入的假 PDF
            if not doc.has_text_layer:
                deal.notes = "扫描件无文本层，需 OCR 并人工复核"
                deal.confidence = "low"
                deal.verdict, deal.verdict_reason = "unclear", "扫描件无文本层"
                return deal

            ex = extractor.extract(row["title"], doc.pages)
            deal.offeror = ex.offeror
            deal.offeror_fa = ex.offeror_fa
            # 标题没写受要约方全称时退回披露易给的简称 —— 那是它自己的归属，
            # 比从正文里猜可靠
            deal.target_full = ex.target or row["name"]
            deal.last_trading_day = ex.last_trading_day
            deal.offer_type = ex.offer_type
            deal.consideration = ex.consideration
            deal.offer_price = ex.offer_price
            deal.deal_size = ex.deal_size
            deal.listing_intent = ex.listing_intent
            deal.confidence = ex.confidence
            deal.verdict, deal.verdict_reason = ex.verdict()
            deal.notes = "；".join(ex.notes)

            comps = [{"anchor": c.anchor, "window": c.window, "label": c.label,
                      "stated_pct": c.stated_pct,
                      "stated_direction": c.stated_direction,
                      "page": c.page, "quote": c.quote} for c in ex.comparisons]
            pick = selectors.select_primary_premium(comps)
            if pick:
                deal.premium_pct = str(pick.signed_pct)
                deal.premium_basis = pick.label

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

            deal.checks = _run_checks(ex, validators)
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
            deal.notes = f"抽取失败：{type(exc).__name__}: {exc}"
            deal.confidence = "low"
            deal.verdict, deal.verdict_reason = "unclear", f"抽取失败：{exc}"
    return deal


def _run_checks(ex, validators) -> str:
    """跑 V4/V5/V6，把结果压成一行。铁律一：算术全在这里，不在抽取层。"""
    from decimal import Decimal

    if not ex.comparisons or not ex.offer_price:
        return ""
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
                                                nonmarket_labels=nonmarket)
    failed = [f"{f.code}:{f.subject}" for f in findings if not f.passed]
    return "全部通过" if not failed else "未通过 " + "；".join(failed[:3])


# 溢价梯子的固定列。做可比表时人人都要按同一口径横向对齐，
# 所以列是固定的：某一单没有这个口径就留空，绝不用别的口径顶上。
LADDER_COLUMNS = [
    "最后交易日收市价", "最后交易日前5日均价", "最后交易日前10日均价",
    "最后交易日前30日均价", "最后交易日前180日均价",
    "未受干扰日收市价", "未受干扰日前5日均价", "未受干扰日前10日均价",
    "未受干扰日前30日均价", "未受干扰日前180日均价",
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
    "要约类型", "条件", "对价形式",
    # 第二层：定价
    "要约价(HKD)", "主值溢价率(%)", "主值口径",
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
            d.offer_type, d.is_conditional, d.consideration,
            d.offer_price, d.premium_pct, d.premium_basis,
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

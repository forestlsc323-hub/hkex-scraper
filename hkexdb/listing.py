"""按日期范围拉取披露易公告列表。

**本模块的检索逻辑照搬自你提供的 `hkex_client.py`（实战验证过的客户端）。**
我原先那版是按接口文档推测写的，有五处硬错误，逐条记在下面，
免得以后有人"顺手改回去"：

| 项目 | 我原来写的（错） | 实战客户端（对） |
| --- | --- | --- |
| 翻页机制 | `pageNo` 递增 | **`rowRange` 递增后重查，按 NEWS_ID 去重** |
| 切块粒度 | 按月 | **按天**（单日全市场 600~800 条） |
| `searchType` | `1` | `0`（日期区间全量检索）|
| `t1code`/`t2Gcode`/`t2code` | `-1` | **`-2`** |
| `lang` | `ZH` | `zh`（小写）|
| 检索策略 | 标题关键词 | **不带关键词，抓当日全量，本地筛** |
| 会话 | 直接打接口 | **先访问检索页建立会话** |

## 翻页机制为什么长这样

披露易这个接口**没有 pageNo**。`rowRange` 是"返回前 N 条"的上限，
要拿更多就把 N 调大重查一次 —— 每次返回的都是从头开始的一整段，
和上一次大量重叠。所以：

    rowRange=100 → 前 100 条
    rowRange=200 → 前 200 条（含刚才那 100 条）

**必须按 NEWS_ID 去重**，否则记录会成倍膨胀。
用 `pageNo` 翻页则根本不生效 —— 服务端忽略这个参数，
每次都返回同一批，循环靠 `hasNextRow` 才会停，结果就是抓到一堆重复或干脆抓不全。

## 为什么按天切

服务端单次查询返回上限约 10000 条（`SERVER_RECORD_CAP`），
超过之后加大 `rowRange` 也没用，只能缩小日期区间。
单日全市场公告约 600~800 条，安全余量很大；按月切会撞上限并**静默截断**。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import logging
import threading
from html import unescape

from . import stocks
from .config import Config
from .http_client import PoliteSession, RateLimiter
from .storage import RawStore, row_uid

log = logging.getLogger(__name__)


class Cancelled(Exception):
    """用户主动停止。GUI 的「停止」按钮靠它中断长任务。"""


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("任务已被用户停止")

BASE = "https://www1.hkexnews.hk"
SEARCH_PAGE = f"{BASE}/search/titlesearch.xhtml"
SERVLET = f"{BASE}/search/titleSearchServlet.do"

# 服务端单次查询返回记录上限。超过后加大 rowRange 无效，只能缩小区间。
SERVER_RECORD_CAP = 10000

# 需要做 HTML 反转义 + tooltip 剥离的字段（接口返回的是带标记的 HTML 片段）
_HTML_FIELDS = ("TITLE", "SHORT_TEXT", "LONG_TEXT", "STOCK_NAME")


def day_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """按天切分。每天一个抓取单元，也是断点续跑的最小粒度。"""
    out = []
    cur = start
    while cur <= end:
        out.append((cur, cur))
        cur += dt.timedelta(days=1)
    return out


def clean_text(value: str) -> str:
    """把接口返回的 HTML 片段还原成纯文本。

    接口给的 TITLE 里带 HTML 实体（`&amp;` `&#x2f;`）、`<br/>`，
    有时还拖着一段 tooltip 的 `<div>...`。不还原的话，
    screening 层的词表匹配会在这些标题上**静默失效** ——
    标题看着对，但 `&amp;` 这种地方一比就不等。

    注意：raw 层仍然保留原始字段，清洗结果另存为 `*_CLEAN` 列，
    两份都在，可随时回溯（铁律：原始层不动，加工层另开）。
    """
    text = unescape(value or "").replace("<br/>", " ").replace("&#x2f;", "/")
    if "<div" in text:                      # 剥掉 tooltip 残留
        text = text.split("<div")[0]
    return " ".join(text.split())


def _build_params(cfg: Config, d1: dt.date, d2: dt.date, row_range: int,
                  *, search_type: str = "0", t1code: str = "-2",
                  t2code: str = "-2", category: str = "0",
                  stock_id: str = "-1") -> dict:
    """参数取值全部照搬实战客户端，不要凭印象改。"""
    return {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": category,
        "market": "SEHK",          # 实测已同时覆盖主板与 GEM
        "stockId": stock_id,
        "documentType": "-1",
        "fromDate": d1.strftime("%Y%m%d"),
        "toDate": d2.strftime("%Y%m%d"),
        "title": "",               # 不带关键词：抓全量，筛选交给 screening 层
        "searchType": search_type,
        "t1code": t1code,
        "t2Gcode": "-2",
        "t2code": t2code,
        "rowRange": str(row_range),
        "lang": "zh",              # 小写
    }


def parse_payload(text: str) -> tuple[list[dict], int, bool, int]:
    """解析接口响应。

    返回 (记录列表, loadedRecord, hasNextRow, recordCnt)。
    `result` 是 JSON 字符串套 JSON，且可能是 None 或字符串 "null"。
    """
    payload = json.loads(text)
    raw = payload.get("result")
    recs = json.loads(raw) if raw not in (None, "null", "") else []
    if isinstance(recs, dict):
        recs = [recs]
    loaded = int(payload.get("loadedRecord", len(recs)) or len(recs))
    total = int(payload.get("recordCnt", loaded) or loaded)
    return recs, loaded, bool(payload.get("hasNextRow")), total


def warm_up_session(session: PoliteSession) -> None:
    """先访问检索页建立会话，拿到 cookie。

    照搬实战客户端 `HKEXClient.__init__` 的做法：检索接口依赖检索页种下的
    cookie，直接打 servlet 容易被拒。

    ⚠️ **必须 `use_cache=False`。** 这里踩过一个坑：
    走缓存的话，第二次运行时检索页会从本地副本返回 ——
    **一个 HTTP 请求都没发出去，cookie 自然也没种上**，
    随后所有 servlet 请求都是裸的。第一次跑得好好的，重跑却静默失效，
    表现是「突然抓不到数据了」而日志上一切正常。
    cookie 是会话状态，不是可缓存的内容。

    失败不致命 —— 实战客户端也是只记警告继续跑。
    """
    try:
        session.get(SEARCH_PAGE, params={"lang": "zh"}, use_cache=False)
        jar = getattr(getattr(session, "session", None), "cookies", None)
        names = sorted(c.name for c in jar) if jar else []
        log.info("会话已建立，cookie：%s", names or "（服务端未下发）")
    except Exception as exc:
        log.warning("建立会话失败（不一定影响检索）：%s", exc)


def fetch_day(session: PoliteSession, store: RawStore, cfg: Config,
              day: dt.date, cancel_event=None) -> int:
    """抓单日全部公告。返回本日去重后的记录数。

    翻页靠加大 rowRange 重查，按 NEWS_ID 去重 —— 见模块头的说明。
    """
    unit = day.isoformat()
    if store.is_done(unit):
        log.info("跳过（已完成）%s", unit)
        return 0

    day_records: dict[str, dict] = {}
    row_range = cfg.row_range_step

    for round_no in range(1, cfg.max_rounds_per_day + 1):
        _check_cancel(cancel_event)
        params = _build_params(cfg, day, day, row_range)
        resp = session.get(SERVLET, params=params)

        try:
            recs, loaded, has_next, total = parse_payload(resp.text)
        except json.JSONDecodeError:
            log.error("%s 第 %d 轮返回的不是 JSON，前 200 字：%r",
                      unit, round_no, resp.text[:200])
            raise

        store.save_page_json(unit, round_no, resp.text)

        before = len(day_records)
        for position, rec in enumerate(recs):
            news_id = str(rec.get("NEWS_ID", "")).strip()
            if not news_id or news_id in day_records:
                continue          # 加大 rowRange 会重复返回前面的记录
            record = dict(rec)    # 原始字段一字不改
            for field in _HTML_FIELDS:
                if field in record:
                    record[f"{field}_CLEAN"] = clean_text(str(record[field]))
            record["_row_uid"] = row_uid("all", unit, round_no, position, news_id)
            record["_query_name"] = "all"
            record["_lang"] = "zh"
            record["_chunk_from"] = unit
            record["_chunk_to"] = unit
            record["_page_no"] = round_no      # 这里是"第几轮 rowRange"，非页码
            record["_row_range"] = row_range
            record["_fetched_at"] = resp.fetched_at
            record["_source_url"] = SERVLET
            day_records[news_id] = record

        log.info("%s 第 %d 轮：rowRange=%s，接口返回 %s 条，新增 %s 条，本日累计 %s 条%s",
                 unit, round_no, row_range, loaded,
                 len(day_records) - before, len(day_records),
                 "（缓存）" if resp.from_cache else "")

        if loaded >= SERVER_RECORD_CAP:
            log.warning("%s 当日公告达到服务端上限 %s 条，可能存在截断！"
                        "需把该日再切细（按半天/按类别）后重抓。",
                        unit, SERVER_RECORD_CAP)

        if not has_next:
            break
        row_range += cfg.row_range_step
    else:
        log.warning("%s 轮到上限 %d 仍未取完，可能是 hasNextRow 判断有误，请检查。",
                    unit, cfg.max_rounds_per_day)

    store.append_rows(list(day_records.values()))
    store.mark_done(unit)
    return len(day_records)


def fetch_category_crosscheck(session: PoliteSession, cfg: Config,
                              t2code: str) -> list[dict]:
    """按收购相关类别代码检索（双保险，只用于比对差集）。

    类别筛选后记录数远小于上限，故按月分段即可，无需按天。

    ⚠️ 这一路**不能单独用作抓取口径**：分类是发行人自己选的，
    归错类的公告会静默漏掉。它的用途是和全量口径比差集 ——
    差集里的东西告诉你词表还缺什么、或者归类有多不可靠。
    """
    results: dict[str, dict] = {}
    cur = cfg.date_from
    while cur <= cfg.date_to:
        nxt = (dt.date(cur.year + 1, 1, 1) if cur.month == 12
               else dt.date(cur.year, cur.month + 1, 1))
        end = min(cfg.date_to, nxt - dt.timedelta(days=1))

        params = _build_params(cfg, cur, end, cfg.row_range_step,
                               search_type="1", t1code=cfg.category_t1code,
                               t2code=t2code)
        resp = session.get(SERVLET, params=params)
        recs, _, _, _ = parse_payload(resp.text)
        for rec in recs:
            news_id = str(rec.get("NEWS_ID", "")).strip()
            if news_id:
                rec["TITLE_CLEAN"] = clean_text(str(rec.get("TITLE", "")))
                results[news_id] = rec
        log.info("类别 %s %s~%s：%d 条", t2code, cur, end, len(recs))
        cur = end + dt.timedelta(days=1)
    return list(results.values())


def segment_days(days: list, n: int) -> list[list]:
    """把天数切成最多 n 个**连续**段，供并发使用。

    切成连续段而非轮流分配，是为了让每个线程的抓取区间在时间上聚集，
    日志读起来是连贯的，中断后也好判断哪一段没做完。
    """
    if not days or n <= 1:
        return [days] if days else []
    size = max(1, (len(days) + n - 1) // n)
    return [days[i:i + size] for i in range(0, len(days), size)]


def make_session(cfg: Config, rate_limiter: RateLimiter | None = None) -> PoliteSession:
    return PoliteSession(
        user_agent=cfg.user_agent,
        cache_dir=cfg.cache_dir,
        min_interval_seconds=cfg.min_interval_seconds,
        timeout_seconds=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
        backoff_base_seconds=cfg.backoff_base_seconds,
        cache_enabled=cfg.cache_enabled,
        rate_limiter=rate_limiter,
    )


def run(cfg: Config, *, max_workers: int = 1, progress_cb=None, cancel_event=None):
    """按配置抓完日期范围内每一天，生成 raw CSV。

    max_workers > 1 时按天切成若干连续段并发抓，**共享一个速率限制器**，
    总请求频率与单线程一致 —— 并发是为了让网络延迟重叠，不是为了压榨服务器。

    progress_cb(已完成天数, 总天数, 累计条数) 每完成一天调用一次（任意线程）。
    cancel_event 是 threading.Event，置位后长任务会尽快抛 Cancelled 退出。
    """
    days = day_chunks(cfg.date_from, cfg.date_to)
    store = RawStore(cfg.raw_dir)
    max_workers = max(1, max_workers)
    log.info("日期范围 %s ~ %s，共 %d 天（按天抓全量公告，不带标题关键词）；线程 %d",
             cfg.date_from, cfg.date_to, len(days), max_workers)

    done_days = 0
    grand_total = 0
    lock = threading.Lock()

    def report(day: dt.date) -> None:
        nonlocal done_days
        with lock:
            done_days += 1
            if done_days % 10 == 0 or done_days == len(days):
                log.info("进度 %d/%d 天，累计新增 %d 条", done_days, len(days), grand_total)
            if progress_cb:
                try:
                    progress_cb(done_days, len(days), grand_total)
                except Exception:      # 回调是外部代码，不能让它拖垮抓取
                    log.debug("progress_cb 抛异常，已忽略", exc_info=True)

    if max_workers == 1:
        session = make_session(cfg)
        warm_up_session(session)
        for d1, _ in days:
            _check_cancel(cancel_event)
            n = fetch_day(session, store, cfg, d1, cancel_event)
            with lock:
                grand_total += n
            report(d1)
        sessions = [session]
    else:
        limiter = RateLimiter(cfg.min_interval_seconds)
        sessions = []

        def work(segment):
            nonlocal grand_total
            sess = make_session(cfg, rate_limiter=limiter)
            warm_up_session(sess)
            with lock:
                sessions.append(sess)
            for d1, _ in segment:
                _check_cancel(cancel_event)
                n = fetch_day(sess, store, cfg, d1, cancel_event)
                with lock:
                    grand_total += n
                report(d1)

        segments = segment_days(days, max_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(segments)) as pool:
            futures = [pool.submit(work, seg) for seg in segments]
            try:
                for future in concurrent.futures.as_completed(futures):
                    future.result()
            except BaseException:
                # 一个段炸了就让其余段尽快停下，别继续对服务器发请求
                if cancel_event is not None:
                    cancel_event.set()
                for future in futures:
                    future.cancel()
                raise

    net = sum(s.stats["network"] for s in sessions)
    hit = sum(s.stats["cache"] for s in sessions)
    retry = sum(s.stats["retries"] for s in sessions)
    log.info("本次新增 %d 条；网络请求 %d 次，缓存命中 %d 次，重试 %d 次",
             grand_total, net, hit, retry)
    return store.rebuild_csv()


# ---------------------------------------------------------------- 按代码检索

def fetch_ticker_history(session: PoliteSession, cfg: Config, ticker: str,
                         stock_map: dict[str, tuple[str, str]],
                         date_from: dt.date | None = None,
                         date_to: dt.date | None = None,
                         cancel_event=None, progress_cb=None) -> list[dict]:
    """按股票代码检索全历史公告（含已除牌证券）。

    移植自实战客户端的 `search_by_ticker`。翻页机制与按天抓取相同：
    加大 rowRange 重查 + 按 NEWS_ID 去重。

    **这是手册第 2 层「公司级完备性对账」的取数入口** ——
    对某家公司，把它的全部公告拉出来，才能确认它的 T0 落在
    「主清单／窗口前／特殊品种／流产案」四个桶的哪一个。
    """
    stock_id, category = stocks.resolve(ticker, stock_map)
    d1 = date_from or dt.date(1999, 1, 1)
    d2 = date_to or dt.date.today()
    log.info("按代码检索 %s（stockId=%s, category=%s）%s ~ %s",
             ticker, stock_id, category, d1, d2)

    results: dict[str, dict] = {}
    row_range = cfg.row_range_step
    total = None

    for round_no in range(1, cfg.max_rounds_per_day + 1):
        _check_cancel(cancel_event)
        params = _build_params(cfg, d1, d2, row_range,
                               search_type="0", t1code="-2", t2code="-2",
                               category=category, stock_id=str(stock_id))
        resp = session.get(SERVLET, params=params)
        recs, loaded, has_next, record_cnt = parse_payload(resp.text)
        if total is None:
            total = record_cnt

        before = len(results)
        for rec in recs:
            news_id = str(rec.get("NEWS_ID", "")).strip()
            if not news_id or news_id in results:
                continue
            record = dict(rec)
            for field in _HTML_FIELDS:
                if field in record:
                    record[f"{field}_CLEAN"] = clean_text(str(record[field]))
            results[news_id] = record

        log.info("%s 第 %d 轮：rowRange=%s，返回 %s 条，新增 %s 条，累计 %s/%s",
                 ticker, round_no, row_range, loaded,
                 len(results) - before, len(results), total)
        if progress_cb:
            try:
                progress_cb(len(results), total or 0)
            except Exception:
                log.debug("progress_cb 抛异常，已忽略", exc_info=True)

        if loaded >= SERVER_RECORD_CAP:
            log.warning("%s 公告数达服务端上限 %s，可能截断！需按年份分段重抓。",
                        ticker, SERVER_RECORD_CAP)
        if not has_next:
            break
        row_range += cfg.row_range_step

    out = list(results.values())
    out.sort(key=lambda r: str(r.get("DATE_TIME", "")), reverse=True)
    return out

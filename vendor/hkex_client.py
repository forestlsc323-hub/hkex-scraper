# -*- coding: utf-8 -*-
"""
披露易(HKEXnews)公告检索客户端
- 按日期区间检索当日全部公告（中文），自动按月切分、自动翻页
- 下载公告PDF
"""
import json
import time
import logging
import datetime as dt
import threading
import concurrent.futures
from html import unescape

import requests

import config

log = logging.getLogger("hkex_client")


class CancelledError(Exception):
    """用户主动停止任务时抛出（与 run_update.CancelledError 同义）"""
    pass


# 披露易单次查询返回记录上限（实测超过后翻页无效，只能缩小区间）
SERVER_RECORD_CAP = 10000

# 披露易证券代码 -> stockId 映射缓存
_STOCK_ID_CACHE = None
_STOCK_LIST_URLS = {
    "inactive": "https://www.hkexnews.hk/ncms/script/eds/inactivestock_sehk_e.json",
    "active": "https://www.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json",
}


class _RateLimiter:
    """跨线程请求速率限制器，保证任意两个请求间隔不小于 min_interval 秒。"""
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.last_request_time = 0.0

    def acquire(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_request_time = time.time()


def _day_chunks(d1: dt.date, d2: dt.date):
    """按天切分区间（单日全市场公告约600-800条，远低于服务端1万条上限）"""
    cur = d1
    while cur <= d2:
        yield cur, cur
        cur += dt.timedelta(days=1)


def _segment_chunks(d1: dt.date, d2: dt.date, n: int):
    """把 [d1,d2] 的每一天切成最多 n 个连续段，每段是 [(day, day), ...]。"""
    days = list(_day_chunks(d1, d2))
    if not days:
        return []
    chunk_size = max(1, (len(days) + n - 1) // n)  # 向上取整
    return [days[i:i + chunk_size] for i in range(0, len(days), chunk_size)]


class HKEXClient:
    def __init__(self, rate_limiter=None):
        self.session = requests.Session()
        self.session.headers.update(config.HEADERS)
        self.rate_limiter = rate_limiter
        # 先访问检索页建立会话
        try:
            if self.rate_limiter:
                self.rate_limiter.acquire()
            self.session.get("https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
                             timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log.warning("初始化会话失败（不影响检索）：%s", e)

    @staticmethod
    def _load_stock_id_map() -> dict:
        """下载并缓存主板/GEM 活跃及已除牌证券的 stockId 映射。"""
        global _STOCK_ID_CACHE
        if _STOCK_ID_CACHE is not None:
            return _STOCK_ID_CACHE
        mapping = {}
        for key, url in _STOCK_LIST_URLS.items():
            try:
                r = requests.get(url, headers=config.HEADERS,
                                 timeout=config.REQUEST_TIMEOUT)
                r.raise_for_status()
                for item in r.json():
                    code = str(item.get("c", "")).strip().lstrip("0").zfill(5)
                    if not code or code == "00000":
                        continue
                    # 活跃证券优先；已除牌证券使用 category=1
                    category = "0" if key == "active" else "1"
                    mapping[code] = (item.get("i"), category)
            except Exception as e:
                log.warning("下载证券列表 %s 失败：%s", url, e)
        _STOCK_ID_CACHE = mapping
        return mapping

    @staticmethod
    def _resolve_stock_id(ticker: str):
        """把 0001 / 0001.HK 解析为 (stockId, category)。"""
        code = (ticker or "").upper().replace(".HK", "").strip()
        code = code.lstrip("0").zfill(5)
        if not code or code == "00000":
            raise ValueError(f"无法识别股票代码：{ticker}")
        mapping = HKEXClient._load_stock_id_map()
        if code not in mapping:
            raise ValueError(
                f"无法在披露易找到标的 {ticker}（可能尚未上市、已除牌或代码错误）")
        return mapping[code]

    @staticmethod
    def _clean(rec: dict) -> dict:
        """清理记录：反转义HTML实体、剥离tooltip残留"""
        out = dict(rec)
        for k in ("TITLE", "SHORT_TEXT", "LONG_TEXT", "STOCK_NAME"):
            v = out.get(k, "") or ""
            v = unescape(v).replace("<br/>", " ").replace("&#x2f;", "/")
            # 剥离 tooltip 的 HTML 片段
            if "<div" in v:
                v = v.split("<div")[0]
            out[k] = " ".join(v.split())
        return out

    def _query_once(self, d1: dt.date, d2: dt.date, row_range: int,
                    search_type: str = "0", t1: str = "-2", t2: str = "-2",
                    category: str = "0", stock_id: str = "-1") -> dict:
        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": category,
            "market": "SEHK",          # 实测该参数已同时覆盖主板与GEM
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": d1.strftime("%Y%m%d"),
            "toDate": d2.strftime("%Y%m%d"),
            "title": "",
            "searchType": search_type,
            "t1code": t1,
            "t2Gcode": "-2",
            "t2code": t2,
            "rowRange": str(row_range),
            "lang": "zh",
        }
        if self.rate_limiter:
            self.rate_limiter.acquire()
        r = self.session.get(config.HKEX_SEARCH_URL, params=params,
                             timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _search_single_day(self, c1: dt.date, c2: dt.date, cancel_event=None) -> dict:
        """检索单日的全部公告，返回 {NEWS_ID: rec}。"""
        day_results = {}
        row_range = config.ROW_RANGE_STEP
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("任务已被用户停止")
            j = self._query_once(c1, c2, row_range)
            raw = j.get("result")
            recs = json.loads(raw) if raw not in (None, "null") else []
            loaded = int(j.get("loadedRecord", len(recs)))
            day_before = len(day_results)
            for rec in recs:
                nid = rec["NEWS_ID"]
                if nid not in day_results:
                    day_results[nid] = self._clean(rec)
            day_after = len(day_results)
            log.info("检索 %s：请求上限 %s 条，接口返回 %s 条，新增 %s 条，本日累计 %s 条",
                     c1, row_range, loaded, day_after - day_before, day_after)
            if loaded >= SERVER_RECORD_CAP:
                log.warning("%s 当日公告达到服务端上限 %s 条，可能存在截断！",
                            c1, SERVER_RECORD_CAP)
            if not j.get("hasNextRow"):
                break
            row_range += config.ROW_RANGE_STEP
            if not self.rate_limiter:
                time.sleep(config.SLEEP_BETWEEN_REQUESTS)
        return day_results

    def _search_days_single_threaded(self, day_chunks, progress_cb=None,
                                     cancel_event=None, n_days: int = 0) -> list:
        """单线程按天抓取，保持与原 search() 一致的行为。"""
        results = {}
        for day_idx, (c1, c2) in enumerate(day_chunks, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("任务已被用户停止")
            day_results = self._search_single_day(c1, c2, cancel_event)
            results.update(day_results)
            log.info("全局累计 %s 条", len(results))
            if progress_cb:
                try:
                    progress_cb(c1, n_days, len(results))
                except Exception:
                    pass
            time.sleep(config.SLEEP_BETWEEN_REQUESTS)
        out = list(results.values())
        out.sort(key=lambda x: x.get("DATE_TIME", ""), reverse=True)
        return out

    def search(self, d1: dt.date, d2: dt.date, progress_cb=None,
               cancel_event=None, max_workers: int = 1) -> list:
        """
        检索 [d1, d2] 区间全部公告（主板+GEM，中文标题）。
        max_workers > 1 时按天切分为最多 max_workers 个连续段并发检索，
        通过全局速率限制器保证总请求频率与单线程一致，避免对服务器造成压力。
        progress_cb(day, total_days, total_count) 在每完成一天时由任意线程触发。
        """
        max_workers = max(1, max_workers)
        n_days = (d2 - d1).days + 1
        segments = _segment_chunks(d1, d2, max_workers)
        if len(segments) <= 1:
            return self._search_days_single_threaded(
                _day_chunks(d1, d2), progress_cb, cancel_event, n_days)

        results = {}
        found_count = 0
        completed_days = 0
        progress_lock = threading.Lock()
        rate_limiter = _RateLimiter(config.SLEEP_BETWEEN_REQUESTS)

        def search_segment(segment):
            nonlocal found_count, completed_days
            local_results = {}
            client = HKEXClient(rate_limiter=rate_limiter)
            for c1, c2 in segment:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("任务已被用户停止")
                day_results = client._search_single_day(c1, c2, cancel_event)
                local_results.update(day_results)
                with progress_lock:
                    found_count += len(day_results)
                    completed_days += 1
                    log.info("全局累计 %s 条（已完成 %s/%s 天）",
                             found_count, completed_days, n_days)
                    if progress_cb:
                        try:
                            progress_cb(c1, n_days, found_count)
                        except Exception:
                            pass
            return local_results

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(search_segment, seg): seg for seg in segments}
            try:
                for future in concurrent.futures.as_completed(futures):
                    segment_results = future.result()
                    for nid, rec in segment_results.items():
                        if nid not in results:
                            results[nid] = rec
            except Exception:
                if cancel_event is not None:
                    cancel_event.set()
                for future in futures:
                    future.cancel()
                raise

        out = list(results.values())
        out.sort(key=lambda x: x.get("DATE_TIME", ""), reverse=True)
        return out

    def search_by_ticker(self, ticker: str, from_date=None, to_date=None,
                         cancel_event=None, progress_cb=None) -> list:
        """
        按股票代码检索全历史公告（含已除牌证券）。
        不限制 MAX_LOOKBACK_DAYS，默认从 1999-01-01 检索至今天。
        """
        stock_id, category = self._resolve_stock_id(ticker)
        d1 = from_date or dt.date(1999, 1, 1)
        d2 = to_date or dt.date.today()
        if isinstance(d1, str):
            d1 = dt.datetime.strptime(d1, "%Y%m%d").date()
        if isinstance(d2, str):
            d2 = dt.datetime.strptime(d2, "%Y%m%d").date()

        log.info("按 ticker 检索：%s (stockId=%s, category=%s) %s ~ %s",
                 ticker, stock_id, category, d1, d2)
        results = {}
        row_range = config.ROW_RANGE_STEP
        total = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("任务已被用户停止")
            j = self._query_once(d1, d2, row_range,
                                 search_type="0", t1="-2", t2="-2",
                                 category=category, stock_id=str(stock_id))
            raw = j.get("result")
            recs = json.loads(raw) if raw not in (None, "null") else []
            loaded = int(j.get("loadedRecord", len(recs)))
            if total is None:
                total = int(j.get("recordCnt", loaded))
            before = len(results)
            for rec in recs:
                nid = rec["NEWS_ID"]
                if nid not in results:
                    results[nid] = self._clean(rec)
            after = len(results)
            log.info("按 ticker 检索 %s：请求上限 %s 条，返回 %s 条，新增 %s 条，累计 %s/%s 条",
                     ticker, row_range, loaded, after - before, after, total)
            if progress_cb:
                try:
                    progress_cb("强制刷新",
                                f"检索 {ticker} 公告 {after}/{total} 条")
                except Exception:
                    pass
            if loaded >= SERVER_RECORD_CAP:
                log.warning("%s 该标的公告达到服务端上限 %s 条，可能存在截断！",
                            ticker, SERVER_RECORD_CAP)
            if not j.get("hasNextRow"):
                break
            row_range += config.ROW_RANGE_STEP
            if not self.rate_limiter:
                time.sleep(config.SLEEP_BETWEEN_REQUESTS)
        out = list(results.values())
        out.sort(key=lambda x: x.get("DATE_TIME", ""), reverse=True)
        return out

    def search_by_category(self, d1: dt.date, d2: dt.date, t2code: str) -> list:
        """按收购相关类别代码检索（双保险，验证用）"""
        results = {}
        cur = d1
        while cur <= d2:
            # 该接口按月分段（类别筛选后记录数远小于上限，无需按天）
            if cur.month == 12:
                nxt = dt.date(cur.year + 1, 1, 1)
            else:
                nxt = dt.date(cur.year, cur.month + 1, 1)
            end = min(d2, nxt - dt.timedelta(days=1))
            j = self._query_once(cur, end, config.ROW_RANGE_STEP,
                                 search_type="1", t1="10000", t2=t2code)
            raw = j.get("result")
            recs = json.loads(raw) if raw not in (None, "null") else []
            for rec in recs:
                results[rec["NEWS_ID"]] = self._clean(rec)
            time.sleep(config.SLEEP_BETWEEN_REQUESTS)
            cur = end + dt.timedelta(days=1)
        return list(results.values())

    def download_pdf(self, file_link: str) -> bytes:
        """下载公告PDF，file_link 形如 /listedco/listconews/sehk/2026/0721/xxx.pdf"""
        url = config.HKEX_BASE_URL + file_link
        r = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        time.sleep(config.SLEEP_BETWEEN_REQUESTS)
        return r.content

    @staticmethod
    def full_url(file_link: str) -> str:
        return config.HKEX_BASE_URL + file_link

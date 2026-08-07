"""与实战客户端 hkex_client.py 的对齐测试。

这份客户端是经过真实环境验证的，本项目的检索行为必须和它一致。
每条测试对应它的一处设计，改坏了立刻会红。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time

import pytest

from hkexdb import listing, stocks
from hkexdb.http_client import RateLimiter
from hkexdb.pdf_source import full_url
from hkexdb.storage import RawStore

from test_listing import FakeHKEX, _make_config, _recs


# ---------------------------------------------------------------- 速率限制器

def test_rate_limiter_serialises_across_threads():
    """并发的目的是让延迟重叠，**不是**提高对服务器的请求频率。

    每个线程各自限速 = 总频率乘以线程数，那是对方会讨厌的行为。
    """
    limiter = RateLimiter(0.05)
    stamps: list[float] = []
    lock = threading.Lock()

    def hit():
        limiter.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=hit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.045 for g in gaps), gaps


def test_shared_limiter_is_used_instead_of_per_session_throttle(tmp_path):
    cfg = _make_config(tmp_path)
    limiter = RateLimiter(0.01)
    s1 = listing.make_session(cfg, rate_limiter=limiter)
    s2 = listing.make_session(cfg, rate_limiter=limiter)
    assert s1.rate_limiter is s2.rate_limiter is limiter


# ---------------------------------------------------------------- 分段

def test_segment_days_splits_into_contiguous_blocks():
    """切成连续段而非轮流分配 —— 日志连贯，中断后好判断哪段没做完。"""
    days = listing.day_chunks(dt.date(2026, 1, 1), dt.date(2026, 1, 10))
    segments = listing.segment_days(days, 3)

    assert sum(len(s) for s in segments) == 10
    for seg in segments:                     # 每段内部日期连续
        for a, b in zip(seg, seg[1:]):
            assert (b[0] - a[0]).days == 1
    flat = [d for seg in segments for d in seg]
    assert flat == days                      # 拼回去还是原顺序，无遗漏无重复


def test_segment_days_handles_edges():
    assert listing.segment_days([], 4) == []
    days = listing.day_chunks(dt.date(2026, 1, 1), dt.date(2026, 1, 2))
    assert listing.segment_days(days, 1) == [days]
    assert sum(len(s) for s in listing.segment_days(days, 10)) == 2


# ---------------------------------------------------------------- 取消

def test_cancel_event_stops_a_running_fetch(tmp_path):
    """GUI 的停止按钮：置位后必须尽快退出，不再对服务器发请求。"""
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: _recs(500)})
    event = threading.Event()
    event.set()

    with pytest.raises(listing.Cancelled):
        listing.fetch_day(session, RawStore(cfg.raw_dir), cfg, day, event)
    assert session.calls == []           # 一个请求都没发出去


def test_cancel_midway_leaves_the_day_unfinished(tmp_path):
    """中途取消的那一天不能被标记完成，否则重跑会跳过它 —— 静默缺数据。"""
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    store = RawStore(cfg.raw_dir)
    event = threading.Event()

    class CancelAfterFirst(FakeHKEX):
        def get(self, url, params=None, headers=None, use_cache=True):
            resp = super().get(url, params, headers, use_cache)
            event.set()                  # 第一轮之后就按下停止
            return resp

    with pytest.raises(listing.Cancelled):
        listing.fetch_day(CancelAfterFirst({day: _recs(500)}), store, cfg, day, event)
    assert not store.is_done(day.isoformat())


# ---------------------------------------------------------------- 进度

def test_progress_callback_fires_once_per_day(tmp_path):
    cfg = _make_config(tmp_path)          # 2026-01-05 ~ 01-06
    session = FakeHKEX({dt.date(2026, 1, 5): _recs(5, "a"),
                        dt.date(2026, 1, 6): _recs(5, "b")})
    seen = []
    restore = _patch(session)
    try:
        listing.run(cfg, progress_cb=lambda done, total, n: seen.append((done, total, n)))
    finally:
        restore()
    assert [d for d, _, _ in seen] == [1, 2]
    assert all(total == 2 for _, total, _ in seen)


def test_a_broken_progress_callback_does_not_kill_the_run(tmp_path):
    """回调是外部代码（GUI）。它抛异常不能把抓取拖垮。"""
    cfg = _make_config(tmp_path)
    session = FakeHKEX({dt.date(2026, 1, 5): _recs(3),
                        dt.date(2026, 1, 6): _recs(3, "b")})

    def boom(*a):
        raise RuntimeError("GUI 挂了")

    restore = _patch(session)
    try:
        _, rows = listing.run(cfg, progress_cb=boom)
    finally:
        restore()
    assert rows == 6


def _patch(fake):
    original = listing.make_session
    listing.make_session = lambda cfg, rate_limiter=None: fake
    return lambda: setattr(listing, "make_session", original)


# ---------------------------------------------------------------- 并发

def test_concurrent_run_covers_every_day_exactly_once(tmp_path):
    cfg = _make_config(tmp_path)
    from hkexdb.config import Config
    cfg = Config(**{**cfg.__dict__, "date_from": dt.date(2026, 1, 1),
                    "date_to": dt.date(2026, 1, 8)})
    by_day = {dt.date(2026, 1, d): _recs(4, f"d{d}") for d in range(1, 9)}
    session = FakeHKEX(by_day)

    restore = _patch(session)
    try:
        _, rows = listing.run(cfg, max_workers=3)
    finally:
        restore()

    assert rows == 32                                   # 8 天 × 4 条
    fetched = [d for d, _ in session.calls]
    assert sorted(set(fetched)) == [f"2026010{d}" for d in range(1, 9)]
    assert len(fetched) == len(set(fetched))            # 每天恰好一次


# ---------------------------------------------------------------- 按代码检索

def test_normalize_code_matches_the_client():
    assert stocks.normalize_code("700") == "00700"
    assert stocks.normalize_code("0700") == "00700"
    assert stocks.normalize_code("00700") == "00700"
    assert stocks.normalize_code("0700.HK") == "00700"
    assert stocks.normalize_code(" 1417 ") == "01417"
    for bad in ("", "0", "00000", None):
        with pytest.raises(ValueError):
            stocks.normalize_code(bad)


def test_active_listing_wins_over_delisted_regardless_of_load_order(tmp_path):
    """活跃证券优先，且**不依赖两份清单的加载顺序**。

    实战客户端靠「后写覆盖先写」实现优先级，能跑但依赖字面顺序 ——
    谁把 _STOCK_LIST_URLS 那两行调换，优先级就静默反过来。
    这里改成显式判断。
    """
    class ListSession:
        stats = {"network": 0, "cache": 0, "retries": 0}

        def get(self, url, params=None, headers=None, use_cache=True):
            # ⚠️ 不能写 `"activestock" in url` —— "inactivestock" 里也含
            # "activestock"，两个 URL 会被判成同一个。和坑②是同一类子串陷阱。
            payload = ([{"c": "00001", "i": "DEAD"}, {"c": "00002", "i": "GONE"}]
                       if "inactivestock" in url else [{"c": "00001", "i": "ACTIVE"}])
            return type("R", (), {"text": json.dumps(payload)})()

    mapping = stocks.load_stock_id_map(ListSession(), tmp_path / "map.json")
    assert mapping["00001"] == ("ACTIVE", "0")     # 活跃的赢
    assert mapping["00002"] == ("GONE", "1")       # 已除牌的照样收录


def test_delisted_companies_must_be_searchable(tmp_path):
    """被要约收购成功的公司往往随后退市 —— 只查活跃清单会漏掉一大批先例。"""
    mapping = {"00001": ("X", "1")}
    stock_id, category = stocks.resolve("0001", mapping)
    assert (stock_id, category) == ("X", "1")


def test_resolve_refuses_to_guess():
    with pytest.raises(ValueError, match="找不到"):
        stocks.resolve("9999", {"00001": ("X", "0")})


def test_ticker_history_uses_the_same_rowrange_pagination(tmp_path):
    """按代码检索的翻页机制与按天抓取相同：rowRange 递增 + NEWS_ID 去重。"""
    cfg = _make_config(tmp_path)

    class TickerFake:
        stats = {"network": 0, "cache": 0, "retries": 0}

        def __init__(self, n):
            self.all = _recs(n)
            self.row_ranges = []

        def get(self, url, params=None, headers=None, use_cache=True):
            rr = int(params["rowRange"])
            self.row_ranges.append(rr)
            page = self.all[:rr]
            from hkexdb.http_client import Response
            return Response(url=url, params=params, status=200, from_cache=False,
                            fetched_at="2026-08-07T00:00:00+00:00",
                            text=json.dumps({
                                "hasNextRow": len(self.all) > rr,
                                "loadedRecord": len(page),
                                "recordCnt": len(self.all),
                                "result": json.dumps(page)}))

    fake = TickerFake(250)
    rows = listing.fetch_ticker_history(fake, cfg, "1417", {"01417": ("SID", "0")})

    assert len(rows) == 250                  # 去重后，不是 550
    assert fake.row_ranges == [100, 200, 300]


def test_ticker_history_is_sorted_newest_first(tmp_path):
    """与客户端一致：按 DATE_TIME 倒序。"""
    cfg = _make_config(tmp_path)

    class Fake:
        stats = {"network": 0, "cache": 0, "retries": 0}

        def get(self, url, params=None, headers=None, use_cache=True):
            recs = [{"NEWS_ID": "a", "DATE_TIME": "2024-01-01 08:00", "TITLE": "旧"},
                    {"NEWS_ID": "b", "DATE_TIME": "2026-06-15 08:00", "TITLE": "新"}]
            from hkexdb.http_client import Response
            return Response(url=url, params=params, status=200, from_cache=False,
                            fetched_at="2026-08-07T00:00:00+00:00",
                            text=json.dumps({"hasNextRow": False, "loadedRecord": 2,
                                             "recordCnt": 2, "result": json.dumps(recs)}))

    rows = listing.fetch_ticker_history(Fake(), cfg, "1417", {"01417": ("SID", "0")})
    assert [r["TITLE"] for r in rows] == ["新", "旧"]


def test_ticker_history_passes_stock_id_and_category(tmp_path):
    """已除牌证券要用 category=1，传错就查不到。"""
    cfg = _make_config(tmp_path)
    captured = {}

    class Fake:
        stats = {"network": 0, "cache": 0, "retries": 0}

        def get(self, url, params=None, headers=None, use_cache=True):
            captured.update(params)
            from hkexdb.http_client import Response
            return Response(url=url, params=params, status=200, from_cache=False,
                            fetched_at="2026-08-07T00:00:00+00:00",
                            text=json.dumps({"hasNextRow": False, "result": "null"}))

    listing.fetch_ticker_history(Fake(), cfg, "0001", {"00001": ("SID9", "1")})
    assert captured["stockId"] == "SID9"
    assert captured["category"] == "1"
    assert captured["searchType"] == "0"


# ---------------------------------------------------------------- PDF 地址

def test_full_url_accepts_a_relative_file_link():
    """raw 表的 FILE_LINK 是相对路径，直接粘进来也要能用。"""
    link = "/listedco/listconews/sehk/2026/0615/2026061500123.pdf"
    assert full_url(link) == "https://www1.hkexnews.hk" + link
    assert full_url(link.lstrip("/")) == "https://www1.hkexnews.hk" + link
    assert full_url("https://x/y.pdf") == "https://x/y.pdf"
    with pytest.raises(ValueError):
        full_url("")


# ---------------------------------------------------------------- 线程安全

def test_checkpoint_survives_concurrent_marking(tmp_path):
    """并发下 checkpoint 是「读-改-写」，不加锁会丢记录 —— 丢了就静默少一天数据。"""
    store = RawStore(tmp_path / "raw")
    units = [f"2026-01-{d:02d}" for d in range(1, 21)]

    threads = [threading.Thread(target=store.mark_done, args=(u,)) for u in units]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(store.is_done(u) for u in units)

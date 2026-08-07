"""列表抓取的离线测试。

不联网：网络层被替换成假的，**并且假的服务端模拟了披露易真实的翻页行为**
—— 这是本文件的重点。接口没有 pageNo，靠加大 rowRange 重查，
每次返回从头开始的一整段，与上次大量重叠。不按 NEWS_ID 去重就会成倍膨胀。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from hkexdb import listing
from hkexdb.config import Config
from hkexdb.http_client import Response, cache_key
from hkexdb.storage import RawStore


# ---------------------------------------------------------------- 切块

def test_day_chunks_is_one_unit_per_day():
    days = listing.day_chunks(dt.date(2026, 1, 1), dt.date(2026, 1, 4))
    assert days == [(dt.date(2026, 1, d), dt.date(2026, 1, d)) for d in (1, 2, 3, 4)]


def test_day_chunks_crosses_month_and_year():
    days = listing.day_chunks(dt.date(2025, 12, 30), dt.date(2026, 1, 2))
    assert len(days) == 4
    assert days[0][0] == dt.date(2025, 12, 30)
    assert days[-1][0] == dt.date(2026, 1, 2)


def test_day_chunks_empty_when_reversed():
    assert listing.day_chunks(dt.date(2026, 3, 1), dt.date(2026, 1, 1)) == []


# ---------------------------------------------------------------- 参数

def test_params_match_the_battle_tested_client():
    """这些取值是实战客户端验证过的，改一个就可能抓不到数据。"""
    cfg = _make_config(None)
    p = listing._build_params(cfg, dt.date(2026, 1, 5), dt.date(2026, 1, 5), 100)

    assert p["searchType"] == "0"       # 日期区间全量检索，不是 "1"
    assert p["t1code"] == "-2"          # 不是 "-1"
    assert p["t2Gcode"] == "-2"
    assert p["t2code"] == "-2"
    assert p["lang"] == "zh"            # 小写
    assert p["title"] == ""             # 不带关键词，抓全量
    assert p["market"] == "SEHK"        # 已覆盖主板与 GEM
    assert p["fromDate"] == p["toDate"] == "20260105"
    assert "pageNo" not in p            # 接口没有这个参数


def test_category_crosscheck_uses_a_different_search_type():
    cfg = _make_config(None)
    p = listing._build_params(cfg, dt.date(2026, 1, 1), dt.date(2026, 1, 31), 100,
                              search_type="1", t1code="10000", t2code="40200")
    assert (p["searchType"], p["t1code"], p["t2code"]) == ("1", "10000", "40200")


# ---------------------------------------------------------------- 解析

def test_parse_payload_unwraps_the_nested_json_string():
    inner = json.dumps([{"NEWS_ID": "1", "TITLE": "要約"}])
    text = json.dumps({"hasNextRow": True, "loadedRecord": 1,
                       "recordCnt": 5, "result": inner})
    recs, loaded, has_next, total = listing.parse_payload(text)
    assert recs == [{"NEWS_ID": "1", "TITLE": "要約"}]
    assert (loaded, has_next, total) == (1, True, 5)


@pytest.mark.parametrize("result", [None, "null", ""])
def test_parse_payload_tolerates_empty_result(result):
    """接口在无结果时会给 None / 字符串 "null" / 空串，三种都要吃下。"""
    recs, loaded, has_next, _ = listing.parse_payload(
        json.dumps({"hasNextRow": False, "result": result}))
    assert recs == [] and loaded == 0 and has_next is False


# ---------------------------------------------------------------- HTML 清洗

def test_clean_text_undoes_html_entities_and_tooltips():
    """接口返回的 TITLE 是 HTML 片段，不还原会让词表匹配静默失效。"""
    assert listing.clean_text("A &amp; B") == "A & B"
    assert listing.clean_text("甲<br/>乙") == "甲 乙"
    assert listing.clean_text("要約&#x2f;收購") == "要約/收購"
    assert listing.clean_text("標題<div class='tip'>提示</div>") == "標題"
    assert listing.clean_text("  多  空格  ") == "多 空格"
    assert listing.clean_text("") == ""


def test_raw_fields_are_kept_alongside_cleaned_ones(tmp_path):
    """铁律：原始层不动，加工层另开。两份都要在。"""
    cfg = _make_config(tmp_path)
    store = RawStore(cfg.raw_dir)
    session = FakeHKEX({dt.date(2026, 1, 5): [
        {"NEWS_ID": "n1", "TITLE": "甲 &amp; 乙<div>tip</div>",
         "STOCK_NAME": "丙&amp;丁", "FILE_LINK": "/x.pdf"}]})

    listing.fetch_day(session, store, cfg, dt.date(2026, 1, 5))
    saved = json.loads(store.rows_path.read_text(encoding="utf-8").strip())

    assert saved["TITLE"] == "甲 &amp; 乙<div>tip</div>"     # 原样
    assert saved["TITLE_CLEAN"] == "甲 & 乙"                  # 清洗后
    assert saved["STOCK_NAME_CLEAN"] == "丙&丁"


# ---------------------------------------------------------------- 假服务端

class FakeHKEX:
    """模拟披露易的真实翻页行为。

    关键点：**没有 pageNo**。rowRange 是「返回前 N 条」的上限，
    每次返回的都是从头开始的一整段，和上一次大量重叠。
    hasNextRow 为真当且仅当还有更多记录没返回。
    """

    def __init__(self, by_day: dict[dt.date, list[dict]]):
        self.by_day = by_day
        self.calls: list[tuple[str, int]] = []
        self.stats = {"network": 0, "cache": 0, "retries": 0}

    def get(self, url, params=None, headers=None, use_cache=True):
        self.stats["network"] += 1
        if params is None or "fromDate" not in params:
            return self._resp(url, params, "<html>search page</html>")

        day = dt.datetime.strptime(params["fromDate"], "%Y%m%d").date()
        row_range = int(params["rowRange"])
        self.calls.append((params["fromDate"], row_range))

        all_recs = self.by_day.get(day, [])
        page = all_recs[:row_range]                    # 永远从第一条开始
        body = json.dumps({
            "hasNextRow": len(all_recs) > row_range,
            "loadedRecord": len(page),
            "recordCnt": len(all_recs),
            "result": json.dumps(page),
        })
        return self._resp(url, params, body)

    @staticmethod
    def _resp(url, params, text):
        return Response(url=url, params=params or {}, status=200, text=text,
                        fetched_at="2026-08-07T00:00:00+00:00", from_cache=False)


def _make_config(tmp_path) -> Config:
    base = tmp_path if tmp_path is not None else __import__("pathlib").Path(".")
    return Config(
        raw={}, config_path=base / "config.yaml",
        data_dir=base / "data", raw_dir=base / "data/raw",
        cache_dir=base / "data/cache", probe_dir=base / "data/probe",
        log_dir=base / "logs",
        date_from=dt.date(2026, 1, 5), date_to=dt.date(2026, 1, 6),
        user_agent="test", min_interval_seconds=0, timeout_seconds=5,
        max_retries=0, backoff_base_seconds=0, cache_enabled=False,
        row_range_step=100, max_rounds_per_day=50,
        category_t1code="10000", category_t2codes=[],
        log_level="INFO",
    )


def _recs(n: int, prefix: str = "n") -> list[dict]:
    return [{"NEWS_ID": f"{prefix}{i}", "DATE_TIME": "2026-01-05 08:00",
             "STOCK_CODE": "00001", "TITLE": f"公告{i}",
             "FILE_LINK": f"/x/{prefix}{i}.pdf"} for i in range(n)]


# ---------------------------------------------------------------- 翻页

def test_row_range_grows_until_all_records_are_in(tmp_path):
    """250 条记录、步长 100 → 需要 3 轮：100 / 200 / 300。"""
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: _recs(250)})
    store = RawStore(cfg.raw_dir)

    count = listing.fetch_day(session, store, cfg, day)

    assert count == 250
    assert [rr for _, rr in session.calls] == [100, 200, 300]


def test_overlapping_rounds_are_deduplicated_by_news_id(tmp_path):
    """这条是本文件最重要的测试。

    每轮返回的记录大量重叠（第 2 轮的前 100 条就是第 1 轮那批）。
    不按 NEWS_ID 去重的话，250 条会变成 100+200+250=550 条。
    """
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: _recs(250)})
    store = RawStore(cfg.raw_dir)

    listing.fetch_day(session, store, cfg, day)
    _, rows = store.rebuild_csv()

    assert rows == 250                    # 不是 550
    lines = store.rows_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 250              # 落盘时就已去重


def test_single_round_when_everything_fits(tmp_path):
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: _recs(30)})
    listing.fetch_day(session, RawStore(cfg.raw_dir), cfg, day)
    assert [rr for _, rr in session.calls] == [100]


def test_empty_day_costs_one_request(tmp_path):
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({})
    assert listing.fetch_day(session, RawStore(cfg.raw_dir), cfg, day) == 0
    assert len(session.calls) == 1


def test_records_without_news_id_are_skipped(tmp_path):
    """NEWS_ID 是去重的唯一依据，没有它的记录无法安全处理。"""
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: [{"NEWS_ID": "", "TITLE": "无 id"},
                              {"NEWS_ID": "ok", "TITLE": "有 id"}]})
    assert listing.fetch_day(session, RawStore(cfg.raw_dir), cfg, day) == 1


# ---------------------------------------------------------------- 断点/幂等

def test_completed_day_is_skipped_on_rerun(tmp_path):
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: _recs(30)})
    store = RawStore(cfg.raw_dir)

    listing.fetch_day(session, store, cfg, day)
    before = len(session.calls)
    listing.fetch_day(session, store, cfg, day)
    assert len(session.calls) == before          # 一次新请求都没发


def test_rebuild_csv_is_byte_identical_across_runs(tmp_path):
    cfg = _make_config(tmp_path)
    day = dt.date(2026, 1, 5)
    store = RawStore(cfg.raw_dir)
    listing.fetch_day(FakeHKEX({day: _recs(150)}), store, cfg, day)

    first = store.rebuild_csv()[0].read_text(encoding="utf-8-sig")
    second = store.rebuild_csv()[0].read_text(encoding="utf-8-sig")
    assert first == second


def test_run_covers_every_day_in_range(tmp_path):
    cfg = _make_config(tmp_path)        # 2026-01-05 ~ 01-06
    session = FakeHKEX({dt.date(2026, 1, 5): _recs(10, "a"),
                        dt.date(2026, 1, 6): _recs(10, "b")})
    monkey = _patch_session(session)
    try:
        _, rows = listing.run(cfg)
    finally:
        monkey()
    assert rows == 20
    assert {d for d, _ in session.calls} == {"20260105", "20260106"}


def _patch_session(fake):
    """把 listing.run 里 new 出来的 PoliteSession 换成假的。"""
    original = listing.PoliteSession
    listing.PoliteSession = lambda **kwargs: fake
    return lambda: setattr(listing, "PoliteSession", original)


# ---------------------------------------------------------------- 上限告警

def test_server_cap_is_logged_as_a_truncation_risk(tmp_path, caplog):
    """单次查询超过 10000 条时服务端会截断，必须告警。

    按月切块就会撞上这个（30 天 × 700 条 ≈ 21000），而且是**静默**截断
    —— 所以本项目按天切。
    """
    cfg = _make_config(tmp_path)
    cfg = Config(**{**cfg.__dict__, "row_range_step": listing.SERVER_RECORD_CAP})
    day = dt.date(2026, 1, 5)
    session = FakeHKEX({day: _recs(listing.SERVER_RECORD_CAP)})

    with caplog.at_level("WARNING"):
        listing.fetch_day(session, RawStore(cfg.raw_dir), cfg, day)
    assert any("上限" in r.message for r in caplog.records)


# ---------------------------------------------------------------- 会话

def test_warm_up_hits_the_search_page_first(tmp_path):
    """接口依赖检索页种下的 cookie，必须先访问它。"""
    session = FakeHKEX({})
    listing.warm_up_session(session)
    assert session.stats["network"] == 1


def test_warm_up_failure_is_not_fatal():
    class Broken:
        stats = {"network": 0, "cache": 0, "retries": 0}

        def get(self, *a, **kw):
            raise RuntimeError("连不上")

    listing.warm_up_session(Broken())      # 不抛异常即通过


# ---------------------------------------------------------------- 缓存键

def test_cache_key_ignores_param_order():
    assert cache_key("u", {"a": "1", "b": "2"}) == cache_key("u", {"b": "2", "a": "1"})

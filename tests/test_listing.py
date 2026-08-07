"""阶段一的离线测试。

这些测试**不联网**：网络那一层被替换成假的，返回预设的 JSON。
目的是验证"拿到数据之后我们怎么处理"这部分逻辑，
特别是工程要求里两条硬指标：幂等、断点续跑。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from hkexdb import listing, probe
from hkexdb.config import Config, Query
from hkexdb.http_client import Response, cache_key
from hkexdb.storage import RawStore


# ---------------------------------------------------------------- 切块

def test_month_chunks_splits_on_calendar_months():
    chunks = listing.month_chunks(date(2026, 1, 1), date(2026, 3, 15))
    assert chunks == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 15)),
    ]


def test_month_chunks_starts_mid_month():
    chunks = listing.month_chunks(date(2026, 1, 20), date(2026, 2, 10))
    assert chunks == [
        (date(2026, 1, 20), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 10)),
    ]


def test_month_chunks_crosses_year_end():
    chunks = listing.month_chunks(date(2025, 12, 1), date(2026, 1, 31))
    assert chunks == [
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 31)),
    ]


def test_month_chunks_multi_month_size():
    chunks = listing.month_chunks(date(2026, 1, 1), date(2026, 6, 30), months=3)
    assert chunks == [
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 6, 30)),
    ]


def test_month_chunks_empty_when_reversed():
    assert listing.month_chunks(date(2026, 3, 1), date(2026, 1, 1)) == []


# ---------------------------------------------------------------- 缓存键

def test_cache_key_ignores_param_order():
    assert cache_key("u", {"a": "1", "b": "2"}) == cache_key("u", {"b": "2", "a": "1"})


def test_cache_key_changes_with_value():
    assert cache_key("u", {"a": "1"}) != cache_key("u", {"a": "2"})


# ---------------------------------------------------------------- 解析

def test_parse_payload_handles_json_string_in_result():
    """接口把结果放在 result 里，且是 JSON 字符串套 JSON。"""
    inner = json.dumps([{"NEWS_ID": "1", "TITLE": "要約"}])
    text = json.dumps({"hasNextRow": "true", "result": inner})
    rows, has_next = listing._parse_payload(text)
    assert rows == [{"NEWS_ID": "1", "TITLE": "要約"}]
    assert has_next is True


def test_parse_payload_handles_plain_list_and_empty():
    rows, has_next = listing._parse_payload(
        json.dumps({"hasNextRow": "false", "result": [{"NEWS_ID": "9"}]}))
    assert rows == [{"NEWS_ID": "9"}] and has_next is False

    rows, has_next = listing._parse_payload(
        json.dumps({"hasNextRow": "false", "result": ""}))
    assert rows == [] and has_next is False


def test_query_params_override_defaults():
    cfg = _make_config(None)
    query = Query(name="cat", lang="ZH", params={"t2code": "40200", "title": "要約"})
    params = listing._build_params(cfg, query, (date(2026, 1, 1), date(2026, 1, 31)), 1)
    assert params["t2code"] == "40200"      # profile 覆盖了默认的 -1
    assert params["title"] == "要約"
    assert params["fromDate"] == "20260101"
    assert params["lang"] == "ZH"


# ---------------------------------------------------------------- 假会话

class FakeSession:
    """替身：按 (查询关键词, 页码) 返回预设数据，并记录被调用了几次。"""

    def __init__(self, pages_by_title: dict[str, list[list[dict]]],
                 fail_on: tuple[str, int] | None = None):
        self.pages_by_title = pages_by_title
        self.fail_on = fail_on
        self.calls: list[tuple[str, int]] = []
        self.stats = {"network": 0, "cache": 0, "retries": 0}

    def get(self, url, params=None, headers=None, use_cache=True):
        title = params["title"]
        page_no = int(params["pageNo"])
        self.calls.append((title, page_no))
        self.stats["network"] += 1

        if self.fail_on == (title, page_no):
            raise RuntimeError("模拟网络中断")

        pages = self.pages_by_title.get(title, [])
        rows = pages[page_no - 1] if page_no <= len(pages) else []
        text = json.dumps({
            "hasNextRow": "true" if page_no < len(pages) else "false",
            "result": json.dumps(rows),
        })
        return Response(url=url, params=params, status=200, text=text,
                        fetched_at="2026-08-07T00:00:00+00:00", from_cache=False)


def _make_config(tmp_path, queries=None) -> Config:
    base = tmp_path if tmp_path is not None else __import__("pathlib").Path(".")
    return Config(
        raw={}, config_path=base / "config.yaml",
        data_dir=base / "data", raw_dir=base / "data/raw",
        cache_dir=base / "data/cache", probe_dir=base / "data/probe",
        log_dir=base / "logs",
        date_from=date(2026, 1, 1), date_to=date(2026, 2, 28),
        user_agent="test", min_interval_seconds=0, timeout_seconds=5,
        max_retries=0, backoff_base_seconds=0, cache_enabled=False,
        chunk_months=1, row_range=100, max_pages_per_chunk=10,
        queries=queries or [Query(name="zh", lang="ZH", params={"title": "要約"})],
        log_level="INFO",
    )


def _rows(*news_ids):
    return [{"NEWS_ID": n, "DATE_TIME": f"2026-01-0{i+1} 08:00",
             "STOCK_CODE": "00001", "TITLE": "要約公告",
             "FILE_LINK": f"/x/{n}.pdf"}
            for i, n in enumerate(news_ids)]


# ---------------------------------------------------------------- 分页/幂等/续跑

def test_fetch_chunk_follows_pagination(tmp_path):
    cfg = _make_config(tmp_path)
    session = FakeSession({"要約": [_rows("a", "b"), _rows("c")]})
    store = RawStore(cfg.raw_dir)

    count = listing.fetch_chunk(session, store, cfg, cfg.queries[0],
                                (date(2026, 1, 1), date(2026, 1, 31)))
    assert count == 3
    assert session.calls == [("要約", 1), ("要約", 2)]


def test_completed_chunk_is_skipped_on_rerun(tmp_path):
    """断点续跑：已完成的时间块不再发请求。"""
    cfg = _make_config(tmp_path)
    session = FakeSession({"要約": [_rows("a")]})
    store = RawStore(cfg.raw_dir)
    chunk = (date(2026, 1, 1), date(2026, 1, 31))

    listing.fetch_chunk(session, store, cfg, cfg.queries[0], chunk)
    calls_after_first = len(session.calls)

    listing.fetch_chunk(session, store, cfg, cfg.queries[0], chunk)
    assert len(session.calls) == calls_after_first     # 一次新请求都没发


def test_interrupted_run_resumes_and_stays_idempotent(tmp_path):
    """中断 → 重跑：结果与"一次跑完"完全一致，不重不漏。"""
    cfg = _make_config(tmp_path)
    pages = {"要約": [_rows("a", "b"), _rows("c", "d")]}
    chunk = (date(2026, 1, 1), date(2026, 1, 31))
    store = RawStore(cfg.raw_dir)

    # 第一次：第 2 页炸了。第 1 页的数据已经落盘，但这块没被标记完成。
    broken = FakeSession(pages, fail_on=("要約", 2))
    with pytest.raises(RuntimeError):
        listing.fetch_chunk(broken, store, cfg, cfg.queries[0], chunk)
    assert not store.is_done(f"zh|2026-01-01_2026-01-31")

    # 第二次：从头重跑这一块，第 1 页的行会被再次追加
    listing.fetch_chunk(FakeSession(pages), store, cfg, cfg.queries[0], chunk)

    _, count = store.rebuild_csv()
    assert count == 4          # 重复追加的第 1 页被 _row_uid 去掉了

    # 与"一次跑完"的干净结果逐字节比对
    clean_dir = tmp_path / "clean"
    clean_store = RawStore(clean_dir)
    listing.fetch_chunk(FakeSession(pages), clean_store, cfg, cfg.queries[0], chunk)
    clean_csv, clean_count = clean_store.rebuild_csv()
    assert clean_count == 4
    assert store.csv_path.read_text(encoding="utf-8-sig") == \
        clean_csv.read_text(encoding="utf-8-sig")


def test_rebuild_csv_is_byte_identical_across_runs(tmp_path):
    """幂等：同样的输入，重复生成 CSV 结果不变。"""
    cfg = _make_config(tmp_path)
    store = RawStore(cfg.raw_dir)
    listing.fetch_chunk(FakeSession({"要約": [_rows("a", "b")]}), store, cfg,
                        cfg.queries[0], (date(2026, 1, 1), date(2026, 1, 31)))

    first = store.rebuild_csv()[0].read_text(encoding="utf-8-sig")
    second = store.rebuild_csv()[0].read_text(encoding="utf-8-sig")
    assert first == second


def test_raw_layer_keeps_all_fields_and_adds_provenance(tmp_path):
    """raw 层不清洗：接口给什么存什么，只追加 _ 开头的出处字段。"""
    cfg = _make_config(tmp_path)
    odd_row = [{"NEWS_ID": "  1  ", "TITLE": "要約  ", "WEIRD_FIELD": "keep me",
                "FILE_LINK": "/x/1.pdf"}]
    store = RawStore(cfg.raw_dir)
    listing.fetch_chunk(FakeSession({"要約": [odd_row]}), store, cfg,
                        cfg.queries[0], (date(2026, 1, 1), date(2026, 1, 31)))

    saved = json.loads(store.rows_path.read_text(encoding="utf-8").strip())
    assert saved["NEWS_ID"] == "  1  "        # 空格原样保留
    assert saved["TITLE"] == "要約  "
    assert saved["WEIRD_FIELD"] == "keep me"  # 没见过的字段也不丢
    assert saved["_query_name"] == "zh"
    assert saved["_page_no"] == 1
    assert saved["_fetched_at"] == "2026-08-07T00:00:00+00:00"


def test_both_languages_are_kept_separately(tmp_path):
    """中英双版是两条 raw 记录，不在 raw 层合并（附录 C 第 8 项）。"""
    queries = [Query(name="zh", lang="ZH", params={"title": "要約"}),
               Query(name="en", lang="EN", params={"title": "OFFER"})]
    cfg = _make_config(tmp_path, queries)
    session = FakeSession({"要約": [_rows("zh1")], "OFFER": [_rows("en1")]})
    store = RawStore(cfg.raw_dir)
    chunk = (date(2026, 1, 1), date(2026, 1, 31))

    for query in queries:
        listing.fetch_chunk(session, store, cfg, query, chunk)

    _, count = store.rebuild_csv()
    assert count == 2


# ---------------------------------------------------------------- 分类挖掘

def test_find_takeover_entries_picks_up_nested_categories():
    taxonomy = {"tier1": [
        {"code": "40000", "name": "公告及通告", "children": [
            {"code": "40200", "name": "收購及合併"},
            {"code": "40300", "name": "季度業績"},
        ]},
    ]}
    hits = probe._find_takeover_entries(taxonomy)
    codes = {code for _, code in hits}
    assert "40200" in codes
    assert "40300" not in codes

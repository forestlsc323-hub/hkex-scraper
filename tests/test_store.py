"""跨次运行存档的测试。

这一层把工具从「每次重头来」变成「一个会长大的库」：
抓过的日子不再抓，抽过的公告不再下 PDF。

风险也集中在这里 —— 复用错了是**静默**的：表照样出，数字却是旧的。
所以两件事必须钉死：存档读回来无损，版本不一致必须重抽。
"""

from __future__ import annotations

import datetime as dt

import pytest

from hkexdb import runner, store


D = dt.date


# ---------------------------------------------------------------- 覆盖范围

def test_days_already_fetched_are_not_fetched_again(tmp_path):
    key = store.coverage_key("keyword", ["要約"])
    store.mark_covered(tmp_path, D(2026, 1, 1), D(2026, 6, 7), key)

    missing = store.missing_days(tmp_path, D(2026, 1, 1), D(2026, 6, 7), key)
    assert missing == []


def test_extending_the_range_only_fetches_the_new_part(tmp_path):
    """你说的那个用法：抓过 2026 之后再要 2025 到今天。"""
    key = store.coverage_key("keyword", ["要約"])
    store.mark_covered(tmp_path, D(2026, 1, 1), D(2026, 6, 7), key)

    missing = store.missing_days(tmp_path, D(2025, 1, 1), D(2026, 6, 10), key)
    ranges = store.to_ranges(missing)

    assert ranges == [(D(2025, 1, 1), D(2025, 12, 31)),
                      (D(2026, 6, 8), D(2026, 6, 10))]
    assert D(2026, 3, 1) not in missing, "2026 那段不该再抓"


def test_a_different_keyword_set_is_a_different_coverage(tmp_path):
    """关键词模式抓过的日子，不等于换一组关键词也抓过 ——
    口径不同，覆盖范围就不同，混在一起会静默漏检。"""
    store.mark_covered(tmp_path, D(2026, 1, 1), D(2026, 1, 31),
                       store.coverage_key("keyword", ["要約"]))
    missing = store.missing_days(
        tmp_path, D(2026, 1, 1), D(2026, 1, 31),
        store.coverage_key("keyword", ["要約", "收購"]))
    assert len(missing) == 31


def test_full_mode_coverage_is_separate_from_keyword_mode(tmp_path):
    store.mark_covered(tmp_path, D(2026, 1, 1), D(2026, 1, 31),
                       store.coverage_key("keyword", ["要約"]))
    missing = store.missing_days(tmp_path, D(2026, 1, 1), D(2026, 1, 31),
                                 store.coverage_key("full", []))
    assert len(missing) == 31, "全量模式会多出一大批标题不含关键词的公告"


def test_to_ranges_groups_contiguous_days():
    days = [D(2026, 1, 1), D(2026, 1, 2), D(2026, 1, 5)]
    assert store.to_ranges(days) == [(D(2026, 1, 1), D(2026, 1, 2)),
                                     (D(2026, 1, 5), D(2026, 1, 5))]
    assert store.to_ranges([]) == []


def test_corrupt_coverage_file_is_treated_as_empty(tmp_path):
    """存档坏了要退回「全都没抓过」，绝不能当成「全都抓过」——
    后者会静默交出一张空表。"""
    (tmp_path / store.STORE_DIR).mkdir(parents=True)
    (tmp_path / store.STORE_DIR / store.COVERAGE_FILE).write_text(
        "{ 这不是 json", encoding="utf-8")
    assert store.load_coverage(tmp_path) == {}
    assert len(store.missing_days(tmp_path, D(2026, 1, 1), D(2026, 1, 3),
                                  "keyword:要約")) == 3


# ---------------------------------------------------------------- 公告列表

def _rec(nid, day="2026-06-01", code="03336"):
    # 披露易给的是 DD/MM/YYYY，夹具必须照它来
    d = dt.date.fromisoformat(day)
    stamp = f"{d.day:02d}/{d.month:02d}/{d.year}"
    return {"NEWS_ID": nid, "DATE_TIME": f"{stamp} 08:00", "STOCK_CODE": code,
            "STOCK_NAME": "巨騰國際", "TITLE": "全面現金要約",
            "FILE_LINK": f"/x/{nid}.pdf"}


def test_listing_accumulates_across_runs(tmp_path):
    assert store.merge_listing(tmp_path, [_rec("a"), _rec("b")]) == (2, 2)
    assert store.merge_listing(tmp_path, [_rec("b"), _rec("c")]) == (1, 3)


def test_listing_can_be_sliced_by_date(tmp_path):
    store.merge_listing(tmp_path, [_rec("a", "2025-06-01"),
                                   _rec("b", "2026-06-01"),
                                   _rec("c", "2026-07-01")])
    rows = store.listing_between(tmp_path, D(2026, 1, 1), D(2026, 6, 30))
    assert [r["NEWS_ID"] for r in rows] == ["b"]


def test_listing_keeps_the_first_version_seen(tmp_path):
    """存档里那条是当初真正见到的样子，后来的重复请求没理由改写它。"""
    store.merge_listing(tmp_path, [_rec("a")])
    changed = _rec("a")
    changed["TITLE"] = "改过的标题"
    store.merge_listing(tmp_path, [changed])
    assert store.load_listing(tmp_path)["a"]["TITLE"] == "全面現金要約"


# ---------------------------------------------------------------- 抽取结果

def _full_deal() -> runner.Deal:
    """字段全填满 —— 存档往返必须一个字段都不丢。"""
    return runner.Deal(
        news_id="n1", verdict="offer", verdict_reason="正文有要约价与价值比较",
        nature="疑似产业收购（待确认）", nature_reasons="主值溢价 +4.49%",
        code="00195", name="綠科科技", target_full="綠科科技國際有限公司",
        offeror="YELLOWSTONE", offeror_fa="華富建業", date="2026-06-15",
        last_trading_day="2024年8月30日", board="GEM",
        offer_type="PO", is_conditional="附先决条件", consideration="现金",
        offer_price="0.40", premium_pct="4.49",
        premium_basis="最后交易日前30日均价",
        premium_ladder={"最后交易日收市价": "42.86",
                        "最后交易日前30日均价": "4.49"},
        deal_size="92000000", listing_intent="拟维持上市",
        total_shares="1366000000", nav_per_share="0.7355", pb_ratio="0.544",
        implied_equity_value="546400000.00", runup_pct="12.34",
        six_month_low="0.28", six_month_high="0.495",
        confidence="high", checks="全部通过", notes="",
        pdf_url="https://x/a.pdf", title="公告 …部分收購要約")


def test_a_deal_survives_a_round_trip_through_the_store():
    """存盘再读回，逐字段比对。

    _deal_row 和 _FROM_ROW 是两张手写的表，任何一边加了列而另一边
    忘了，存档读回来就静默丢字段 —— 表照样出，只是少了几个数。
    """
    deal = _full_deal()
    row = dict(zip(runner.DEAL_COLUMNS, runner._deal_row(deal)))
    row["NEWS_ID"] = deal.news_id
    back = runner._deal_from_row(row)

    for attr in vars(deal):
        if attr == "evidence":
            continue          # 引文单独存在 evidence.json
        assert getattr(back, attr) == getattr(deal, attr), f"字段「{attr}」丢了"


def test_the_whole_premium_ladder_survives_the_round_trip():
    deal = _full_deal()
    row = dict(zip(runner.DEAL_COLUMNS, runner._deal_row(deal)))
    assert runner._deal_from_row(row).premium_ladder == deal.premium_ladder


def test_a_matching_version_is_reusable():
    assert store.reusable({"抽取器版本": store.EXTRACTOR_VERSION})


def test_a_stale_version_is_not_reused():
    """我改了正则却复用旧结果，表面一切正常，数字却是旧逻辑抽的 ——
    这正是铁律二说的静默污染。宁可重抽。"""
    assert not store.reusable({"抽取器版本": "2020-01-A"})
    assert not store.reusable({})


def test_deals_accumulate_and_newer_wins(tmp_path):
    cols = runner.DEAL_COLUMNS
    store.merge_deals(tmp_path, [{"NEWS_ID": "a", "抽取器版本": "v1",
                                  "要约类型": "MGO"}], cols)
    total = store.merge_deals(tmp_path, [{"NEWS_ID": "a", "抽取器版本": "v2",
                                          "要约类型": "VGO"},
                                         {"NEWS_ID": "b", "抽取器版本": "v2"}], cols)
    assert total == 2
    assert store.load_deals(tmp_path)["a"]["要约类型"] == "VGO"


def test_evidence_accumulates_and_is_never_lost(tmp_path):
    """原件用完即弃时，这份引文是审计链上唯一剩下的东西。"""
    store.merge_evidence(tmp_path, {"https://x/a.pdf": {"要约价": [3, "引文A"]}})
    store.merge_evidence(tmp_path, {"https://x/b.pdf": {"要约价": [5, "引文B"]}})
    loaded = store.load_evidence(tmp_path)
    assert loaded["https://x/a.pdf"]["要约价"] == [3, "引文A"]
    assert loaded["https://x/b.pdf"]["要约价"] == [5, "引文B"]


# ---------------------------------------------------------------- 端到端

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    (tmp_path / "screening_rules.yaml").write_bytes(
        (runner.Path(__file__).parent.parent / "screening_rules.yaml").read_bytes())
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_second_run_sends_no_requests_at_all(isolated):
    """同一段日期跑第二遍，抓取和抽取都该是零工作量。"""
    from tests.test_runner import fake_open_pdf, fake_records

    calls, opened = [], []

    def fetch(d1, d2, log, on_step, cancel_event):
        calls.append((d1, d2))
        return fake_records(10)

    def opener(url):
        opened.append(url)
        return fake_open_pdf(url)

    first = runner.run(D(2026, 6, 1), D(2026, 6, 7), fetch=fetch, open_pdf=opener)
    assert first.ok and calls and opened

    calls.clear(), opened.clear()
    second = runner.run(D(2026, 6, 1), D(2026, 6, 7), fetch=fetch, open_pdf=opener)

    assert second.ok
    assert calls == [], "第二遍还在抓公告列表"
    assert opened == [], "第二遍还在下 PDF"
    assert len(second.deals) == len(first.deals)


def test_widening_the_range_only_fetches_the_new_days(isolated):
    from tests.test_runner import fake_open_pdf, fake_records

    calls = []

    def fetch(d1, d2, log, on_step, cancel_event):
        calls.append((d1, d2))
        return fake_records(4, day=d1.isoformat())

    runner.run(D(2026, 6, 1), D(2026, 6, 7), fetch=fetch, open_pdf=fake_open_pdf)
    calls.clear()
    runner.run(D(2026, 5, 25), D(2026, 6, 7), fetch=fetch, open_pdf=fake_open_pdf)

    assert calls == [(D(2026, 5, 25), D(2026, 5, 31))], \
        "应该只补抓前面那一小段，6/1~6/7 已经在存档里"


def test_a_new_extractor_version_forces_a_re_extract(isolated, monkeypatch):
    """我改了抽取逻辑，旧结果必须作废重抽 —— 这是「规则变更重跑」。"""
    from tests.test_runner import fake_open_pdf, fake_records

    opened = []

    def opener(url):
        opened.append(url)
        return fake_open_pdf(url)

    runner.run(D(2026, 6, 1), D(2026, 6, 7),
               fetch=lambda *a: fake_records(10), open_pdf=opener)
    assert opened
    opened.clear()

    monkeypatch.setattr(store, "EXTRACTOR_VERSION", "9999-新版本")
    runner.run(D(2026, 6, 1), D(2026, 6, 7),
               fetch=lambda *a: fake_records(10), open_pdf=opener)
    assert opened, "换了抽取器版本却还在复用旧结果 —— 静默污染"


# ---------------------------------------------------------------- 日期格式

@pytest.mark.parametrize("raw,want", [
    ("04/06/2026 16:35", "2026-06-04"),     # 披露易真实格式 DD/MM/YYYY
    ("26/03/2026", "2026-03-26"),
    ("01/01/2026 09:00", "2026-01-01"),
    ("2026-06-04 16:35", "2026-06-04"),     # ISO 也要认
    ("2026/06/04", "2026-06-04"),
    ("", ""),
    ("看不懂", ""),
    ("32/13/2026", ""),                      # 非法日期不许瞎猜
])
def test_hkex_dates_are_really_parsed_not_string_swapped(raw, want):
    """披露易给的是 DD/MM/YYYY，不是 ISO。

    我当初只把斜杠换成横杠就拿去比大小 ——「04-06-2026」按字符串
    排在「2026-01-01」前面，2594 条公告一条都过不了日期筛子，
    日志上还写着「新增 2594 条进存档」。日期绝不能靠字符串替换糊弄。
    """
    assert store.normalise_date(raw) == want


def test_a_real_hkex_date_range_filter_keeps_the_rows(tmp_path):
    """这条就是那次 0 条事故的最小复现。"""
    store.merge_listing(tmp_path, [
        {"NEWS_ID": "a", "DATE_TIME": "04/06/2026 16:35", "STOCK_CODE": "01657",
         "STOCK_NAME": "樺欣控股", "TITLE": "要約", "FILE_LINK": "/x/a.pdf"},
        {"NEWS_ID": "b", "DATE_TIME": "18/05/2026 08:00", "STOCK_CODE": "03336",
         "STOCK_NAME": "巨騰國際", "TITLE": "要約", "FILE_LINK": "/x/b.pdf"},
        {"NEWS_ID": "c", "DATE_TIME": "15/11/2025 08:00", "STOCK_CODE": "00001",
         "STOCK_NAME": "範圍外", "TITLE": "要約", "FILE_LINK": "/x/c.pdf"},
    ])
    rows = store.listing_between(tmp_path, D(2026, 1, 1), D(2026, 6, 7))
    assert [r["NEWS_ID"] for r in rows] == ["a", "b"], "日期筛子又把数据丢了"


def test_the_iso_column_is_derived_not_overwriting_the_raw_one():
    """原始层不动，加工层另开 —— DATE_TIME 一字不改，另存 DATE_ISO。"""
    assert "DATE_TIME" in store.LISTING_COLUMNS
    assert "DATE_ISO" in store.LISTING_COLUMNS


def test_an_old_store_without_the_iso_column_still_filters(tmp_path):
    """上一版存下来的 listing.csv 没有 DATE_ISO，不能因此读不出来。"""
    d = tmp_path / store.STORE_DIR
    d.mkdir(parents=True)
    (d / store.LISTING_FILE).write_text(
        "NEWS_ID,DATE_TIME,STOCK_CODE,STOCK_NAME,TITLE,FILE_LINK,FIRST_SEEN\n"
        "a,04/06/2026 16:35,01657,樺欣控股,要約,/x/a.pdf,2026-08-10\n",
        encoding="utf-8-sig")
    rows = store.listing_between(tmp_path, D(2026, 1, 1), D(2026, 6, 7))
    assert [r["NEWS_ID"] for r in rows] == ["a"]


def test_fetching_rows_but_keeping_none_is_a_loud_failure(isolated):
    """抓到 2594 条、筛完剩 0 条 —— 上一版就这么无声地把数据丢了。

    这在逻辑上说不通，只可能是日期解析错了，所以必须炸，
    不能安安静静地交出一张空表还显示「完成」。
    """
    bad = [{"NEWS_ID": "a", "DATE_TIME": "日期格式坏了", "STOCK_CODE": "01",
            "STOCK_NAME": "甲", "TITLE": "要約", "FILE_LINK": "/x/a.pdf"}]
    result = runner.run(D(2026, 1, 1), D(2026, 6, 7),
                        fetch=lambda *a: bad, open_pdf=lambda u: None)
    assert not result.ok
    assert "日期解析对不上" in result.error


def test_force_refetch_ignores_the_archive(isolated):
    """存档说「抓过了」但结果不对时，得有一条退路能强制重来。"""
    from tests.test_runner import fake_open_pdf, fake_records

    calls = []

    def fetch(d1, d2, log, on_step, cancel_event):
        calls.append((d1, d2))
        return fake_records(6)

    runner.run(D(2026, 6, 1), D(2026, 6, 7), fetch=fetch, open_pdf=fake_open_pdf)
    calls.clear()

    runner.run(D(2026, 6, 1), D(2026, 6, 7), fetch=fetch,
               open_pdf=fake_open_pdf)
    assert calls == [], "没勾强制时不该重抓"

    runner.run(D(2026, 6, 1), D(2026, 6, 7), fetch=fetch,
               open_pdf=fake_open_pdf, force_refetch=True)
    assert calls == [(D(2026, 6, 1), D(2026, 6, 7))], "勾了强制却没重抓"

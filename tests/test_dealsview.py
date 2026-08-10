"""要约明细表数据层的测试。

界面本身在这个环境里画不出来（没有 tkinter），但界面里除了摆控件
之外的每一件事都在 dealsview 里，所以这些测试覆盖的就是
「点下去之后表格会变成什么样」。
"""

from __future__ import annotations

import json

import pytest

from hkexdb import dealsview as D
from hkexdb import runner


def make_deal(**kw) -> runner.Deal:
    base = dict(code="03336", name="巨騰國際", target_full="巨騰國際控股有限公司",
                offeror="藍思科技股份有限公司", offeror_fa="中信里昂證券有限公司",
                date="2026-05-18", offer_type="VGO", consideration="现金",
                offer_price="2.20", premium_pct="-15.45",
                premium_basis="未受干扰日前30日均价",
                premium_ladder={"最后交易日收市价": "-45.68",
                                "未受干扰日前30日均价": "-15.45"},
                deal_size="1905849908.60", listing_intent="拟维持上市",
                confidence="high", checks="全部通过",
                pdf_url="https://example.invalid/a.pdf", title="聯合公告 …")
    base.update(kw)
    return runner.Deal(**base)


def rows(*deals) -> list[dict]:
    return D.rows_from_deals(list(deals))


# ---------------------------------------------------------------- 取数一致性

def test_csv_and_memory_produce_the_same_shape(tmp_path, monkeypatch):
    """刚跑完 和 重启后重新载入，必须走出同一张表。

    两条路径长出两套字段名是界面最容易出的一类 bug：跑完好好的，
    第二天打开就一片空白。
    """
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    deals = [make_deal()]
    runner._write_deals(deals, lambda *_: None)

    from_memory = D.rows_from_deals(deals)[0]
    from_csv = D.load_rows(tmp_path / "data" / "deals.csv")[0]
    assert set(from_memory) == set(from_csv)
    assert from_memory == from_csv


def test_every_visible_column_actually_exists_in_the_data():
    """表头写了一个字段名，数据里却没有 —— 那一列会整列空白。"""
    row = rows(make_deal())[0]
    for field, *_ in D.COLUMNS:
        assert field in row, f"表头有「{field}」，但数据里没有这个字段"


def test_load_rows_on_a_missing_file_is_empty_not_a_crash():
    """第一次打开程序时 deals.csv 还不存在。"""
    assert D.load_rows("/nonexistent/deals.csv") == []
    assert D.load_evidence("/nonexistent/e.json") == {}


def test_corrupt_evidence_file_does_not_kill_the_window(tmp_path):
    bad = tmp_path / "e.json"
    bad.write_text("{ 这不是 json", encoding="utf-8")
    assert D.load_evidence(bad) == {}


# ---------------------------------------------------------------- 排序

def test_deal_size_sorts_as_a_number_not_as_text():
    """按字符串排，9 会排在 1,905,849,908 后面 —— 表面正常的错。"""
    small, big = make_deal(deal_size="92000000"), make_deal(deal_size="1905849908.60")
    out = D.sort_rows(rows(small, big), "交易规模(HKD)", reverse=True)
    assert out[0]["交易规模(HKD)"] == "1905849908.60"


def test_premium_sorts_negative_below_positive():
    """折让是负数。按字符串排「-55.57」会跑到「4.49」前面还是后面全看运气。"""
    out = D.sort_rows(rows(make_deal(premium_pct="4.49"),
                           make_deal(premium_pct="-55.57")),
                      "主值溢价率(%)")
    assert [r["主值溢价率(%)"] for r in out] == ["-55.57", "4.49"]


def test_rows_without_a_value_sink_to_the_bottom():
    """抽不到的那几单不该因为排序跑到表头去误导人。"""
    out = D.sort_rows(rows(make_deal(premium_pct=""),
                           make_deal(premium_pct="4.49")),
                      "主值溢价率(%)", reverse=True)
    assert out[-1]["主值溢价率(%)"] == ""


# ---------------------------------------------------------------- 筛选

def test_search_matches_any_column_including_the_offeror():
    """做 precedent 最常见的动作就是「这个买方还买过谁」。"""
    data = rows(make_deal(), make_deal(offeror="YOMI.SUN HOLDING LIMITED",
                                       code="01417"))
    assert len(D.filter_rows(data, "藍思")) == 1
    assert len(D.filter_rows(data, "yomi")) == 1        # 大小写不敏感
    assert len(D.filter_rows(data, "")) == 2


def test_type_filter_and_search_compose():
    data = rows(make_deal(offer_type="VGO"),
                make_deal(offer_type="MGO", offeror="YOMI.SUN"))
    assert len(D.filter_rows(data, "", "MGO")) == 1
    assert len(D.filter_rows(data, "藍思", "MGO")) == 0


# ---------------------------------------------------------------- 显示

def test_deal_size_gets_thousand_separators_without_changing_the_number():
    """千分位是显示，不是计算 —— 小数位一位都不许丢（铁律一）。"""
    assert D.thousands("1905849908.60") == "1,905,849,908.60"
    assert D.thousands("92000000") == "92,000,000"
    assert D.thousands("") == ""
    assert D.thousands("待补") == "待补"


# ---------------------------------------------------------------- 明细面板

def test_detail_shows_the_whole_premium_ladder_not_just_the_primary():
    """下一个用这张表的人可能要按「最后交易日收市价」重排，
    只给主值等于把选择权拿走了。"""
    text = D.detail_text(rows(make_deal())[0])
    assert "最后交易日收市价" in text and "-45.68" in text
    assert "未受干扰日前30日均价" in text and "-15.45" in text


def test_no_comparison_is_silently_dropped_from_the_table():
    """公告用了固定列以外的口径，也必须落到「其他比较项」里。

    1417 的「規則3.7 公告前收市價」和 3336 的「前180日均价」当初就是
    这么无声消失的：表看着完整，实际少了两条比较。
    """
    deal = make_deal(premium_ladder={"最后交易日收市价": "-45.68",
                                     "某个没见过的口径": "-9.99"})
    row = rows(deal)[0]
    assert "某个没见过的口径" in row[runner.LADDER_OTHER]
    assert "-9.99" in D.detail_text(row)


def test_the_ladder_covers_every_label_the_extractor_can_emit():
    """抽取层能产出的口径，表里必须都有对应的列（除非落进兜底列）。"""
    from hkexdb.extractor import Comparison

    for anchor in ("undisturbed", "last_trading_day", "pre_rule37", "nav"):
        for window in ("spot", "5d", "10d", "30d", "180d", "nav"):
            label = Comparison(anchor=anchor, window=window, benchmark="1",
                               benchmark_decimals=0, benchmark_is_exact=True,
                               stated_pct="1", stated_direction="premium",
                               page=1, quote="x").label
            row = rows(make_deal(premium_ladder={label: "1.00"}))[0]
            assert (f"较{label}(%)" in row and row[f"较{label}(%)"]) or \
                   label in row[runner.LADDER_OTHER], f"口径「{label}」在表里消失了"


def test_the_primary_premium_is_marked_in_the_ladder():
    text = D.detail_text(rows(make_deal())[0])
    line = next(ln for ln in text.splitlines() if "未受干扰日前30日均价" in ln)
    assert "←主值" in line


def test_detail_shows_source_quotes_with_page_numbers():
    """铁律三：有值就要能追到原文第几页。"""
    row = rows(make_deal())[0]
    evidence = {row["PDF链接"]: {"要约价": [3, "「要約價」 指 每股要約股份2.20港元"]}}
    text = D.detail_text(row, evidence)
    assert "第 3 页" in text and "2.20港元" in text


def test_missing_fields_are_shown_as_missing_not_as_blank():
    """空白格看起来像「没有这一项」，实际是「没抽到」。这两件事不一样。"""
    text = D.detail_text(rows(make_deal(listing_intent=""))[0])
    line = next(ln for ln in text.splitlines() if "上市地位意向" in ln)
    assert "未抽到" in line


def test_detail_on_no_selection_explains_what_to_do():
    assert "点一行" in D.detail_text({})


def test_summary_counts_by_type():
    text = D.summary(rows(make_deal(offer_type="VGO"),
                          make_deal(offer_type="MGO"),
                          make_deal(offer_type="MGO")))
    assert "共 3 单" in text and "MGO 2" in text and "VGO 1" in text


def test_summary_on_empty_tells_the_user_what_to_do():
    assert "抓取" in D.summary([])


# ---------------------------------------------------------------- 出处存盘

def test_evidence_survives_a_restart(tmp_path):
    """重启后没有出处就等于违背铁律三。CSV 塞不下引文，故单独存。"""
    deal = make_deal()
    deal.evidence = {"要约价": [3, "「要約價」 指 每股要約股份2.20港元"]}
    path = tmp_path / "data" / "deals_evidence.json"
    D.save_evidence([deal], path)

    loaded = D.load_evidence(path)
    assert loaded[deal.pdf_url]["要约价"] == [3, "「要約價」 指 每股要約股份2.20港元"]
    assert json.loads(path.read_text(encoding="utf-8"))     # 是合法 JSON


def test_run_writes_the_evidence_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    deal = make_deal()
    deal.evidence = {"要约价": [3, "引文"]}
    runner._write_deals([deal], lambda *_: None)
    assert (tmp_path / "data" / "deals_evidence.json").exists()


# ---------------------------------------------------------------- 列的完整性

@pytest.mark.parametrize("field", ["要约方", "要约方财务顾问", "受要约方",
                                   "要约类型", "要约价(HKD)", "主值溢价率(%)",
                                   "主值口径", "交易规模(HKD)"])
def test_the_columns_an_ib_actually_uses_are_all_present(field):
    """做港股 precedent 时这几列缺一不可 —— 尤其是「主值口径」：
    一个 -15.45% 不写明相对什么，放进可比表就是污染。"""
    assert field in runner.DEAL_COLUMNS


def test_premium_basis_sits_next_to_the_premium_number():
    """口径和数字必须相邻，中间插别的列就会有人只复制数字。"""
    i = runner.DEAL_COLUMNS.index("主值溢价率(%)")
    assert runner.DEAL_COLUMNS[i + 1] == "主值口径"


# ---------------------------------------------------------------- .htm 公告

def test_html_announcements_are_accepted_too():
    """留存桶里有 .htm 公告 —— 实测一周 15 份里有 4 份是，全部抽取失败。

    披露易短公告发的是 HTML，正文措辞和 PDF 版一模一样。
    """
    from hkexdb import pdf_source

    assert pdf_source.is_html_link("https://x/a_c.htm")
    assert pdf_source.is_html_link("https://x/a.HTML")
    assert not pdf_source.is_html_link("https://x/a.pdf")


def test_html_is_stripped_down_to_the_announcement_text():
    from hkexdb import pdf_source

    html_bytes = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<script>var x=1</script>"
        "<p>「要約價」 指 每股要約股份2.20港元</p>"
        "<p>要約人就悉數接納要約而應付之最高現金金額為1,905,849,908.60港元。</p>"
        "</body></html>").encode("utf-8")
    pages = pdf_source.extract_html_pages(html_bytes)

    text = " ".join(pages.values())
    assert "2.20港元" in text and "1,905,849,908.60港元" in text
    assert "var x" not in text and "color:red" not in text


def test_html_announcement_flows_through_the_extractor():
    """HTML 版走完抽取层，字段要和 PDF 版抽出来的一样。"""
    from hkexdb import extractor, pdf_source

    html_bytes = (
        "<html><body><p>「要約價」 指 提出要約所按之價格，即每股要約股份2.20港元</p>"
        "<p>要約人就悉數接納要約而應付之最高現金金額為1,905,849,908.60港元。</p>"
        "</body></html>").encode("utf-8")
    ex = extractor.extract("公告 自願性有條件全面現金要約",
                           pdf_source.extract_html_pages(html_bytes))
    assert ex.offer_type == "VGO"
    assert ex.offer_price == "2.20"
    assert ex.deal_size == "1905849908.60"


def test_html_and_pdf_use_different_cache_files():
    """两种格式共用一个缓存名会互相覆盖，重跑就拿到错的字节。"""
    from hkexdb import pdf_source

    p = pdf_source._cache_path(runner.Path("/tmp/c"), "https://x/a_c.htm")
    q = pdf_source._cache_path(runner.Path("/tmp/c"), "https://x/a.pdf")
    assert p.suffix == ".htm" and q.suffix == ".pdf"

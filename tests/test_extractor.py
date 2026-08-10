"""抽取层测试。

用例文本全部照抄 1417 / 3336 / 00195 三份真实公告的原文
（fixtures 里有完整出处），期望值是人工核对过的答案。
"""

from __future__ import annotations

import pytest

from hkexdb import extractor, selectors

# ---- 三份真实公告的「价值比较」原文（照抄，一字未改）----

P1417 = {
    1: "「要約價」 應付現金金額每股要約股份0.519港元",
    2: "價值比較每股要約價為每股0.519港元，較："
       "(i) 本公司根據收購守則規則3.7於2026年5月18日發佈公告前的最後一個交易日"
       "2026年5月15日在聯交所所報價的收市價每股0.840港元折讓約38.21%；"
       "(ii) 股份於最後交易日在聯交所所報收市價每股1.870港元折讓約72.25%；"
       "(iii) 股份於緊接最後交易日（包括該日）前五(5)個連續交易日在聯交所所報"
       "平均收市價每股1.914港元折讓約72.88%；"
       "(iv) 股份於緊接最後交易日（包括該日）前十(10)個連續交易日在聯交所所報"
       "平均收市價每股1.849港元折讓約71.93%；"
       "(v) 股份於緊接最後交易日（包括該日）前三十(30)個連續交易日在聯交所所報"
       "平均收市價每股約1.168港元折讓約55.57%；以及"
       "(vi) 本公司於2025年12月31日的每股經審計合併淨資產價值約0.348的港元"
       "溢價約49.14%。"
       "最高與最低股價股份在聯交所所報最高收市價為於2026年6月8日的每股1.980港元，"
       "及股份在聯交所所報最低收市價為每股0.200港元。",
    3: "要約人於要約項下須支付的最高現金代價約為5,440萬港元。",
}

# 3336：两套锚点，且第 7 项和第 8 项之间夹着页脚「- 11 -」
P3336 = {
    1: "「要約價」 指 提出要約所按之價格，即每股要約股份2.20港元",
    2: "價值比較要約價為每股要約股份2.20港元，較："
       "1. 最後交易日於聯交所所報之收市價每股股份4.05港元折讓約45.68%；"
       "2. 未受干擾日於聯交所所報之收市價每股股份3.18港元折讓約為30.82%；"
       "3. 緊接最後交易日（包括該日）前5個交易日於聯交所所報之平均收市價"
       "約每股股份3.18港元折讓約30.73%；"
       "4. 緊接未受干擾日（包括該日）前5個交易日於聯交所所報之平均收市價"
       "約每股股份2.89港元折讓約23.82%；"
       "5. 緊接最後交易日（包括該日）前10個交易日於聯交所所報之平均收市價"
       "約每股股份2.76港元折讓約20.23%；"
       "6. 緊接未受干擾日（包括該日）前10個交易日於聯交所所報之平均收市價"
       "約每股股份2.61港元折讓約15.64%；"
       "7. 緊接最後交易日（包括該日）前30個交易日於聯交所所報之平均收市價"
       "約每股股份2.67港元折讓約17.50%；\n- 11 -\n"
       "8. 緊接未受干擾日（包括該日）前30個交易日於聯交所所報之平均收市價"
       "約每股股份2.60港元折讓約15.45%；"
       "9. 緊接最後交易日（包括該日）前180個交易日於聯交所所報之平均收市價"
       "約每股股份1.83港元溢價約19.90%；"
       "10. 緊接未受干擾日（包括該日）前180個交易日於聯交所所報之平均收市價"
       "約每股股份1.82港元溢價約20.91%；及"
       "11. 本集團於二零二五年十二月三十一日每股約3.73港元的經審核之股東應佔綜合"
       "淨資產（按(i)於本聯合公告日期合共1,200,008,445股股份及(ii)本集團於"
       "二零二五年十二月三十一日經審核之股東應佔綜合淨資產約4,475,770,000港元"
       "計算）折讓約41.02%。"
       "最高及最低股價股份在聯交所所報最高收市價為每股4.05港元，"
       "股份在聯交所所報最低收市價為每股1.62港元。"
       "確認具備充足財務資源要約人就悉數接納要約而應付之最高現金金額為"
       "1,905,849,908.60港元。",
}

P00195 = {
    1: "「要約價」 指 將以現金作出部分收購要約的每股要約股份價格0.40港元",
    2: "要約價的價值比較要約價每股要約股份0.40港元較："
       "(i) 股份於2024年8月30日（即最後交易日）在聯交所所報收市價每股0.28港元"
       "溢價約42.86%；"
       "(ii) 股份於截至最後交易日（包括該日）止最後連續五個交易日在聯交所所報"
       "平均收市價每股0.401港元折讓約0.25%；"
       "(iii) 股份於截至最後交易日（包括該日）止最後連續十個交易日在聯交所所報"
       "平均收市價每股0.3925港元溢價約1.91%；"
       "(iv) 股份於截至最後交易日（包括該日）止最後連續三十個交易日在聯交所所報"
       "平均收市價每股約0.3828港元溢價約4.49%；及"
       "(v) 截至2023年12月31日的每股經審核受要約公司擁有人應佔綜合資產淨值"
       "約0.7355港元折讓約45.62%。"
       "最高及最低股價股份於聯交所所報的最高收市價為每股0.495港元；"
       "及股份於聯交所所報的最低收市價為每股0.28港元。"
       "要約人根據部分收購要約購買230,000,000股要約股份須支付的現金代價總額為"
       "92,000,000港元。",
}

CASES = [
    ("1417", P1417, "聯合公告 (2) 作出強制性無條件現金要約",
     dict(type="MGO", price="0.519", n=6, premium="-55.57", size="54400000")),
    ("3336", P3336, "聯合公告 具有前置條件之自願性有條件全面現金要約",
     dict(type="VGO", price="2.20", n=11, premium="-15.45", size="1905849908.60")),
    ("00195", P00195, "公告 提出附帶先決條件的自願現金部分收購要約",
     dict(type="PO", price="0.40", n=5, premium="4.49", size="92000000")),
]


def _premium(ex):
    comps = [{"anchor": c.anchor, "window": c.window, "label": c.label,
              "stated_pct": c.stated_pct, "stated_direction": c.stated_direction,
              "page": c.page, "quote": c.quote} for c in ex.comparisons]
    pick = selectors.select_primary_premium(comps)
    return str(pick.signed_pct) if pick else None


@pytest.mark.parametrize("name,pages,title,want", CASES,
                         ids=[c[0] for c in CASES])
def test_all_four_fields_match_the_human_answers(name, pages, title, want):
    """四个主字段必须和人工核对过的答案一致。"""
    ex = extractor.extract(title, pages)
    assert ex.offer_type == want["type"]
    assert ex.offer_price == want["price"]
    assert len(ex.comparisons) == want["n"]
    assert _premium(ex) == want["premium"]
    assert ex.deal_size == want["size"]


# ---------------------------------------------------------------- 踩过的坑

def test_inline_parenthesised_numbers_are_not_item_markers():
    """条文里的「前五(5)個」「前三十(30)個」不是条目编号。

    当成编号会把句子从中间劈开，窗口识别不出来 —— 三十日均价会被判成收市价，
    于是主值溢价率取到完全不同的一条。
    """
    ex = extractor.extract("", P1417)
    windows = [c.window for c in ex.comparisons]
    assert windows == ["spot", "spot", "5d", "10d", "30d", "nav"]


def test_page_footer_does_not_glue_two_items_together():
    """3336 的第 7、8 项之间夹着页脚「- 11 -」。

    不去掉页脚，第 8 项切不出来、与第 7 项粘连；粘连后那一条同时含
    「最後交易日」和「未受干擾日」，锚点会判错 —— 产出一个看着正常的错数字。
    这条测试守着的是**抽错**，不是漏抽。
    """
    ex = extractor.extract("", P3336)
    thirty = [c for c in ex.comparisons if c.window == "30d"]
    assert len(thirty) == 2
    assert {c.anchor for c in thirty} == {"last_trading_day", "undisturbed"}
    by_anchor = {c.anchor: c.stated_pct for c in thirty}
    assert by_anchor["last_trading_day"] == "17.50"
    assert by_anchor["undisturbed"] == "15.45"


def test_nested_roman_numerals_inside_an_item_are_ignored():
    """NAV 那条里嵌了「（按(i)…及(ii)…計算）」，不能被当成新条目。"""
    ex = extractor.extract("", P3336)
    nav = [c for c in ex.comparisons if c.anchor == "nav"]
    assert len(nav) == 1
    assert nav[0].stated_pct == "41.02"


def test_undisturbed_anchor_wins_the_primary_premium():
    """3336 有两套锚点。取错锚点差 2.05 个百分点。"""
    ex = extractor.extract("", P3336)
    assert _premium(ex) == "-15.45"        # 不是 -17.50


def test_approx_marker_decides_whether_the_benchmark_is_exact():
    """「約X港元」是约整值，「X港元」是精确值。

    这个标记直接决定 V4 能不能用区间检验 —— 记错会造成大批假警报。
    """
    ex = extractor.extract("", P1417)
    by_window = {c.window: c for c in ex.comparisons}
    assert by_window["spot"].benchmark_is_exact is True        # 1.870
    assert by_window["30d"].benchmark_is_exact is False        # 約1.168


def test_chinese_unit_is_converted_and_plain_number_is_kept():
    """「約5,440萬港元」要换算；印全的数字要原样保留，不许取整。"""
    assert extractor.extract("", P1417).deal_size == "54400000"
    assert extractor.extract("", P3336).deal_size == "1905849908.60"


def test_offer_type_comes_from_the_title_not_word_frequency():
    """3336 全文 11 次「無條件」都不是在描述类型，词频法会判反。"""
    noisy = dict(P3336)
    noisy[3] = "要約未必會成為或獲宣佈為無條件要約。" * 5
    ex = extractor.extract("聯合公告 具有前置條件之自願性有條件全面現金要約", noisy)
    assert ex.offer_type == "VGO"


# ---------------------------------------------------------------- 抽不到时

def test_missing_fields_are_left_blank_with_notes():
    """铁律三：抽不到就留空并说明，绝不填一个看起来合理的数。"""
    ex = extractor.extract("董事會會議日期", {1: "本公司謹訂於下週召開董事會會議。"})
    assert ex.offer_type == ""
    assert ex.offer_price == ""
    assert ex.deal_size == ""
    assert ex.comparisons == []
    assert ex.confidence == "low"
    assert ex.notes


def test_confidence_reflects_how_much_was_extracted():
    full = extractor.extract("聯合公告 強制性無條件現金要約", P1417)
    assert full.confidence == "high"        # 四项全抽到

    # 同一份公告，标题不带类型 → 类型抽不到 → 降为 medium，如实反映
    assert extractor.extract("", P1417).confidence == "medium"
    assert extractor.extract("", {1: "無關內容"}).confidence == "low"


def test_every_extracted_field_carries_page_and_quote():
    """铁律三：有值就必须有出处。"""
    ex = extractor.extract("聯合公告 強制性無條件現金要約", P1417)
    assert ex.offer_price and ex.offer_price_evidence.quote
    assert ex.deal_size and ex.deal_size_evidence.page > 0
    for c in ex.comparisons:
        assert c.page > 0 and c.quote.strip()

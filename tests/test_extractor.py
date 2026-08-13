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


def test_approx_marker_is_found_on_either_side_of_每股():
    """3336 写的是「約每股股份3.18港元」——「約」在「每股」**左边**。

    只认「每股約X」会把 3336 的 8 个基准全判成精确值，V4 的区间检验
    退化成等式检验，11 项里 8 项报假警报。这条守着的是那 8 个假警报。
    """
    ex = extractor.extract("", P3336)
    exact = {c.label: c.benchmark_is_exact for c in ex.comparisons}
    assert exact["最后交易日前5日均价"] is False        # 約每股股份3.18
    assert exact["未受干扰日前30日均价"] is False       # 約每股股份2.60
    assert exact["最后交易日收市价"] is True            # 每股股份4.05，无「約」


def test_the_whole_ladder_survives_recomputation():
    """V4 用公告自己的基准价复算公告自己的百分比，三单一条都不许挂。

    这条是「铁律一」的落点：模型只摘录，Python 复算交叉验证。
    真挂了说明摘错了，不是公告错了。
    """
    from decimal import Decimal

    from hkexdb import validators

    for pages in (P1417, P3336, P00195):
        ex = extractor.extract("", pages)
        comps = [validators.PriceComparison(
            label=c.label, benchmark=Decimal(c.benchmark),
            benchmark_decimals=c.benchmark_decimals,
            benchmark_is_exact=c.benchmark_is_exact,
            stated_pct=Decimal(c.stated_pct),
            stated_direction=c.stated_direction,
            page=c.page, source_quote=c.quote) for c in ex.comparisons]
        nonmarket = frozenset(c.label for c in ex.comparisons if c.anchor == "nav")
        findings = validators.run_price_comparisons(
            Decimal(ex.offer_price), comps,
            Decimal(ex.six_month_low or 0), Decimal(ex.six_month_high or 10 ** 9),
            nonmarket_labels=nonmarket)
        failed = [f"{f.code}:{f.subject}" for f in findings if not f.passed]
        assert not failed, failed


# ---------------------------------------------------------------- 当事方

PARTY_CASES = [
    ("1417",
     "聯合公告 (1) 完成出售及購買浦江中國控股有限公司擬出售股份 (2) 由力高證券有限公司"
     "為並代表 YOMI.SUN HOLDING LIMITED 就收購浦江中國控股有限公司全部已發行股份"
     "作出強制性無條件現金要約 及 (3) 恢復股份買賣",
     "YOMI.SUN HOLDING LIMITED", "力高證券有限公司", "浦江中國控股有限公司"),
    ("3336",
     "聯合公告 (1)有關本公司已發行股份總數約27.81%的買賣協議 (2)中信里昂證券有限公司"
     "代表藍思科技股份有限公司提出具有前置條件之自願性有條件全面現金要約 "
     "(3)藍思科技股份有限公司之須予披露交易 及 (4)復牌",
     "藍思科技股份有限公司", "中信里昂證券有限公司", ""),      # 标题没写受要约方
    ("00195",
     "公告 由華富建業企業融資有限公司代表 YELLOWSTONE INTERNATIONAL LIMITED "
     "提出附帶先決條件的自願現金部分收購要約以收購綠科科技國際有限公司的"
     "不超過230,000,000股股份",
     "YELLOWSTONE INTERNATIONAL LIMITED", "華富建業企業融資有限公司",
     "綠科科技國際有限公司"),
]


@pytest.mark.parametrize("name,title,offeror,fa,target", PARTY_CASES,
                         ids=[c[0] for c in PARTY_CASES])
def test_parties_are_cut_out_of_the_title(name, title, offeror, fa, target):
    """港股要约标题是固定句式：由[FA]（為並）代表[要约人]就收購[标的]…

    做 precedent 时「谁买谁、谁做的 FA」比溢价率还先看。
    """
    ex = extractor.extract(title, {})
    assert ex.offeror == offeror
    assert ex.offeror_fa == fa
    assert ex.target == target


def test_the_second_收購_does_not_get_eaten_into_the_target_name():
    """00195 标题里有两处「收購」：「部分收購要約以收購綠科…」。

    从第一处起非贪婪匹配，会把「要約以收購」一起吃进公司名里，
    产出「要約以收購綠科科技國際有限公司」这种看着还挺像的错答案。
    """
    ex = extractor.extract(PARTY_CASES[2][1], {})
    assert ex.target == "綠科科技國際有限公司"
    assert "要約" not in ex.target


def test_missing_offeror_is_flagged_not_guessed():
    ex = extractor.extract("董事會會議日期", {1: "本公司謹訂於下週召開董事會會議。"})
    assert ex.offeror == "" and ex.offeror_fa == "" and ex.target == ""
    assert any("要约方" in n for n in ex.notes)


def test_listing_intent_is_only_reported_when_the_pdf_says_so():
    """「没写撤销上市」不等于「维持上市」—— 那样的默认值就是编造。"""
    assert extractor.extract("", P1417).listing_intent == ""
    assert extractor.extract(
        "", {1: "要約人擬維持本公司的上市地位。"}).listing_intent == "拟维持上市"
    assert extractor.extract(
        "", {1: "要約人擬撤銷本公司的上市地位。"}).listing_intent == "拟撤销上市"


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


def test_fa_name_with_parentheses_is_not_chopped():
    """券商名带括号是常态：中國銀河國際證券(香港)有限公司、建銀國際(控股)有限公司。

    按任意括号切左边界，FA 会被切成「有限公司」——实测 01657 那单就是
    这么错的，表里显示的要约方FA 是「有限公司」，看着像抽到了。
    只认编号括号（里面纯数字或罗马数字）。
    """
    ex = extractor.extract(
        "聯合公告 (1) 買賣協議 及 (2) 由中國銀河國際證券(香港)有限公司代表 "
        "ABC LIMITED 提出強制性無條件現金要約", {})
    assert ex.offeror_fa == "中國銀河國際證券(香港)有限公司"
    assert ex.offeror == "ABC LIMITED"


def test_generic_offeror_word_falls_back_to_the_definition_section():
    """标题写「代表要約人提出…」时，「要約人」是通称不是名字。

    表里出现一个叫「要約人」的要约方比留空更糟 —— 它看着像抽到了。
    真名在释义节里，退过去找。
    """
    ex = extractor.extract(
        "公告 由某證券有限公司代表要約人提出強制性無條件現金要約",
        {1: "「要約人」 指 樺欣投資控股有限公司，一家於英屬處女群島註冊成立之公司。"})
    assert ex.offeror == "樺欣投資控股有限公司"
    assert ex.parties_evidence.page == 1


# ------------------------------------------------ 分段解析的粗筛（只管要不要往下翻）

def test_the_probe_never_misses_a_real_offer_announcement():
    """粗筛判错的代价是不对称的：多解析几份没用的只是慢一点，
    漏判一份就是整单丢掉。所以这里拿真实公告的首页措辞逐条守。
    """
    covers = [
        "強制性無條件現金要約以收購全部已發行股份",
        "要約價為每股要約股份 0.519 港元",
        "自願有條件現金要約",
        "部分要約",
        "建議以協議安排方式將公司私有化 註銷價每股 63.70 港元",
        "價值比較 較最後交易日收市價溢價約 15.4%",
        "根據收購守則第 3.5 條作出的公告",
        "本聯合公告乃由要約人及本公司聯合發出",
    ]
    for text in covers:
        assert extractor.looks_like_offer({1: text}), text


def test_the_probe_says_no_to_an_ordinary_notice():
    """真正该被挡住的：占了留存桶大头的普通停复牌与程序公告。"""
    for text in ["董事會會議召開日期", "根據上市規則第 13.51B 條作出的公告",
                 "截至二零二六年六月三十日止六個月之中期業績", "更換公司秘書"]:
        assert not extractor.looks_like_offer({1: text}), text


def test_the_probe_reads_across_line_breaks():
    """PDF 会把一句话拦腰劈开。粗筛读的必须是拼回去的文本，
    否则「每股要約股份\\n0.519 港元」这种就漏了。"""
    assert extractor.looks_like_offer({1: "每股要約股份\n0.519\n港元"})


def test_an_alias_in_the_definition_term_is_not_glued_onto_the_name():
    """释义表是两栏排版，扁平化后词条和定义直接连在一起，而词条常带别名。

    08439 新百利那单实跑出来的要约方是「或「買方」 Sky Links Group
    Limited」—— 左栏那句「或「買方」」被当成名字的一部分抽了进去。
    这种脏名字比留空更坏：它看着像抽对了，粘贴进底稿也不会有人发现。
    """
    ex = extractor.extract(
        "公告 由某證券有限公司代表要約人提出強制性無條件現金要約",
        {1: "「要約人」或「買方」 Sky Links Group Limited，"
            "一家於英屬處女群島註冊成立之有限公司"})
    assert ex.offeror == "Sky Links Group Limited"


def test_the_next_definition_term_does_not_leak_into_the_name():
    """两栏排版扁平化后，下一行的词条会直接顶在上一行定义的屁股后面。

    01780 榮尊那单抽出来是「楊敬堯先生「海外股東」」—— 后半截是
    释义表下一行的词条。名字里不该出现「」，见到就是越界了，切掉。
    """
    ex = extractor.extract(
        "公告 由某證券有限公司代表要約人提出強制性無條件現金要約",
        {1: "「要約人」 楊敬堯先生「海外股東」 指 於香港境外之股東"})
    assert ex.offeror == "楊敬堯先生"


def test_generic_offeror_with_no_definition_stays_empty():
    """释义节里也没有真名，就留空 —— 绝不把通称当名字填进去。"""
    ex = extractor.extract("公告 由某證券有限公司代表要約人提出要約", {})
    assert ex.offeror == ""
    assert any("要约方" in n for n in ex.notes)


# ---------------------------------------------------------------- 正文层判定

def test_a_real_offer_is_judged_an_offer():
    assert extractor.extract("", P1417).verdict()[0] == "offer"
    assert extractor.extract("", P3336).verdict()[0] == "offer"
    assert extractor.extract("", P00195).verdict()[0] == "offer"


def test_a_plain_trading_halt_notice_is_judged_not_an_offer():
    """实跑一周，留存桶 15 条里 13 条是这种 —— 标题带「復牌」而已。

    表里摆 13 行空白看起来像程序坏了。判出来、标出来，人一眼跳过。
    """
    verdict, reason = extractor.extract(
        "恢復買賣", {1: "應本公司要求，本公司股份已於今日上午九時正起恢復買賣。"}
    ).verdict()
    assert verdict == "not_offer"
    assert "價值比較" in reason


def test_half_extracted_is_flagged_for_a_human_not_silently_dropped():
    """只抽到一半 —— 可能是真要约但格式特别，绝不能当成非要约扔掉。"""
    verdict, reason = extractor.extract(
        "", {1: "「要約價」 指 每股要約股份1.50港元"}).verdict()
    assert verdict == "unclear"
    assert "人工" in reason


# ---------------------------------------------------------------- 估值组

def test_six_month_range_survives_a_date_in_the_middle():
    """1417 写的是「最高收市價為**於2026年6月8日的**每股1.980港元」。

    中间那个日期里有数字，原来的正则不许出现数字，被整段挡掉 ——
    1417 和 00195 的六个月区间就是这么丢的，而丢了它 V6 就没得校验。
    """
    ex = extractor.extract("", P1417)
    assert ex.six_month_low == "0.200" and ex.six_month_high == "1.980"


def test_nav_is_available_separately_from_the_premium():
    """NAV 不做溢价率主值，但要单独取得到 —— 壳股看 P/B。"""
    assert extractor.extract("", P3336).nav_per_share == "3.73"
    assert extractor.extract("", P00195).nav_per_share == "0.7355"


def test_both_anchor_spots_are_reachable_for_the_leak_check():
    """算泄露涨幅要拿到两个锚点的收市价。"""
    ex = extractor.extract("", P3336)
    assert ex.spot("undisturbed") == "3.18"
    assert ex.spot("last_trading_day") == "4.05"


def test_conditionality_comes_from_the_title():
    """无条件 MGO ＝ 已成事实；有条件 ＝ 还要判断能不能成。"""
    assert extractor.extract(
        "聯合公告 作出強制性無條件現金要約", {}).is_conditional == "无条件"
    assert extractor.extract(
        "聯合公告 具有前置條件之自願性有條件全面現金要約", {}).is_conditional == "附先决条件"


def test_debt_conversion_leaves_a_trace_with_a_quote():
    """债转股被动触发 26.1 是技术性要约的标志，必须留出处。"""
    ex = extractor.extract("", {1: "本公司將發行可換股債券，換股價為每股1.00港元。"})
    assert ex.debt_conversion
    assert ex.debt_conversion_evidence.quote


def test_no_debt_conversion_on_a_plain_cash_offer():
    assert not extractor.extract("", P1417).debt_conversion


def test_total_shares_is_extracted_not_computed():
    """股数是摘的，乘法是 Python 做的（铁律一）。"""
    ex = extractor.extract("", P3336)
    assert ex.total_shares == "1200008445"
    assert ex.total_shares_evidence.page > 0


# ---------------------------------------------------------------- 交易规模

def test_a_per_share_price_is_never_taken_as_the_deal_size():
    """实跑里 02362 金川國際的交易规模抽成了 0.01 —— 那是每股价。

    「倘要約獲悉數接納，按每股要約股份0.01港元計算」被最松的那条兜底
    规则抓中了。每股价是单价，交易规模是总额，两者永远不能互相顶替。
    答案表里那单是 7,000,000。
    """
    for text in ["倘要約獲悉數接納，按每股要約股份0.01港元計算",
                 "假設要約獲全數接納，每股0.01港元",
                 "倘要約獲悉數接納，價格為每股0.40港元"]:
        got, _ = extractor.extract_deal_size({1: text})
        assert got == "", f"把每股价当成了交易规模：{got}　原文：{text}"


def test_the_real_total_is_still_found_in_the_same_sentence_shape():
    got, _ = extractor.extract_deal_size(
        {1: "倘要約獲悉數接納，要約人須支付的最高現金代價為7,000,000港元。"})
    assert got == "7000000"


def test_deal_size_is_never_smaller_than_the_offer_price():
    """总额小于单价在算术上说不通 —— 这类错必须看得出来。"""
    ex = extractor.extract("", {
        1: "「要約價」 指 每股要約股份0.01港元",
        2: "價值比較要約價每股0.01港元較最後交易日收市價每股0.62港元折讓約98.38%。"
           "倘要約獲悉數接納，要約人須支付的最高現金代價為7,000,000港元。"})
    assert ex.offer_price == "0.01"
    assert float(ex.deal_size) > float(ex.offer_price)


# ---------------------------------------------------------------- 面值陷阱

def test_par_value_is_never_mistaken_for_the_offer_price():
    """「每股面值 0.01 港元」满篇都是，和要约价长得一模一样。

    02362 金川國際实跑抽出要约价 0.01、溢价 -98.38% —— 那不是要约价，
    是股份面值。数字还「自洽」（0.01 对 0.617 确实是 -98.4%），
    所以校验器一个都拦不住，人看报表也只当这单折让离谱。
    错得很像对的，这正是铁律二说的静默污染。
    """
    price, _ = extractor.extract_offer_price(
        {1: "本公司股本中每股面值0.01港元之普通股。"
            "要約價為每股要約股份0.617港元。"})
    assert price == "0.617"


def test_par_value_written_mid_sentence_is_also_blocked():
    """「要約價…每股面值0.01港元」这种写法会从中间绕过开头的挡板。"""
    price, _ = extractor.extract_offer_price(
        {1: "要約價指就每股要約股份（每股面值0.01港元）應付之現金價格。"})
    assert price != "0.01"


def test_a_normal_offer_price_still_comes_through():
    for text, want in [
        ("「要約價」 指 每股要約股份0.519港元", "0.519"),
        ("要約價為每股要約股份 2.20 港元", "2.20"),
        ("每股要約股份0.276港元", "0.276"),
    ]:
        assert extractor.extract_offer_price({1: text})[0] == want, text


# ---------------------------------------------------------------- 规模的量级闸

def test_a_deal_size_equal_to_the_per_share_price_is_thrown_away():
    """总代价不可能等于每股价 —— 定义上就不可能。

    实跑 02362 那一行是「PO 0.01 -98.38% 0.01」：要约价和交易规模
    一模一样。留一个假数比留空坏得多，留空会被人补上，
    错数会被人直接粘进底稿。
    """
    ex = extractor.Extraction()
    ex.offer_price, ex.deal_size = "0.01", "0.01"
    extractor._drop_impossible_deal_size(ex)

    assert ex.deal_size == ""
    assert any("不相称" in n for n in ex.notes), "作废了必须说一声"


def test_a_real_deal_size_survives_the_gate():
    ex = extractor.Extraction()
    ex.offer_price, ex.deal_size = "0.519", "54400000"
    extractor._drop_impossible_deal_size(ex)
    assert ex.deal_size == "54400000"


def test_the_gate_stays_quiet_when_either_number_is_missing():
    """抽不到就是抽不到，不该因为缺一个数就再作废另一个。"""
    for price, size in [("", "54400000"), ("0.519", ""), ("", "")]:
        ex = extractor.Extraction()
        ex.offer_price, ex.deal_size = price, size
        extractor._drop_impossible_deal_size(ex)
        assert ex.deal_size == size and not ex.notes


def test_a_generic_subscriber_is_not_taken_as_the_offeror():
    """08220 比高集團实跑抽出要约方「認購人可能」。

    「認購人」和「要約人」一样是通称，配股清洗豁免那类公告里满篇都是；
    后面还粘了个助动词「可能」。表里出现一个叫「認購人可能」的要约方，
    比留空糟得多 —— 它看着像抽到了。
    """
    ex = extractor.extract(
        "公告 由某證券有限公司代表認購人可能須提出強制性無條件現金要約", {})
    assert ex.offeror == ""
    assert any("要约方" in n for n in ex.notes)


def test_a_real_name_is_not_chopped_by_the_new_boundaries():
    """新加的边界词不能误伤真名字。"""
    ex = extractor.extract(
        "聯合公告 由中國銀河國際證券(香港)有限公司代表 ABC LIMITED "
        "提出強制性無條件現金要約", {})
    assert ex.offeror == "ABC LIMITED"


# ---------------------------------------------------------------- 类型判错

def test_the_traditional_form_of_partial_is_recognised():
    """「部份」和「部分」在港交所公告里混用，后者是简体习惯。

    09638 法拉帝实跑被判成 VGO，答案是 PO —— 标题写的是「部份」。
    类型判错是分类层的错，静默污染（铁律二）。
    """
    for title in ["公告 提出附帶先決條件的自願現金部份收購要約",
                  "公告 提出附帶先決條件的自願現金部分收購要約"]:
        assert extractor.extract_offer_type(title, {})[0] == "PO", title


def test_mandatory_beats_partial_when_both_words_appear():
    """收購守則規則 26 的强制要约必须就**全部**股份提出 —— 不可能同时
    是部分要约。两个词一起出现时「強制性」说了算，「部分」多半出现在
    别处（「部分股東已承諾接納」）。01796 实跑被判成 PO，答案是 MGO。
    """
    title = ("聯合公告 強制性無條件現金要約 及 部分股東之不可撤銷承諾")
    assert extractor.extract_offer_type(title, {})[0] == "MGO"


def test_a_genuine_partial_offer_is_still_a_partial_offer():
    """别为了修上一条把真的部分要约压没了。"""
    assert extractor.extract_offer_type(
        "公告 提出附帶先決條件的自願現金部分收購要約", {})[0] == "PO"


def test_a_half_chopped_role_word_is_not_an_offeror():
    """08220 实跑抽出要约方「益人」—— 一个被切了半截的通称。

    比整个通称更危险：「認購人」一眼能认出是通称，「益人」看着像个名字。
    """
    for junk in ["益人", "認購人", "受益人", "承配人"]:
        assert extractor._is_placeholder(junk), junk


def test_real_names_are_not_mistaken_for_role_words():
    for name in ["楊敬堯先生", "Sky Links Group Limited", "大成國際控股有限公司",
                 "香港偉業軟件股份有限公司"]:
        assert not extractor._is_placeholder(name), name


def test_a_whitewash_waiver_does_not_turn_a_partial_offer_into_an_mgo():
    """「強制性全面要約」最常见的出处其实是**清洗豁免**：

        申請豁免…須提出強制性全面要約的責任

    那句话说的是这单**不必**做强制要约。拿它去压过「部分要约」正好压反 ——
    修一个类型错的时候造出另一个类型错，是这一层最容易犯的病。
    """
    title = ("公告 (1) 自願現金部分收購要約 及 "
             "(2) 申請豁免須提出強制性全面要約的責任")
    assert extractor.extract_offer_type(title, {})[0] == "PO"


def test_rule_26_1_outranks_a_stray_partial_in_the_body():
    """規則26.1 是强制性全面要约的**法律依据**，比正文里蹦出来的措辞硬。

    实跑 01980 / 01796 / 02362 三单被正文里的「部分」判成 PO，答案都是
    MGO —— 那些「部分」多半出在「部分股東已承諾接納」这种句子里。
    """
    pages = {1: "本公司股東部分已作出不可撤銷承諾。要約人須根據收購守則"
                "規則26.1就全部已發行股份提出要約。"}
    assert extractor.extract_offer_type("聯合公告 及 恢復買賣", pages)[0] == "MGO"


def test_an_explicit_partial_offer_title_still_beats_rule_26_1():
    """标题写明「部分收購要約」是最权威的证据 ——
    而几乎每份收购文件的释义节都会顺带提到 26.1，不能让它翻盘。"""
    pages = {1: "釋義：「收購守則」指公司收購及合併守則，包括規則26.1。"}
    assert extractor.extract_offer_type(
        "公告 提出附帶先決條件的自願現金部分收購要約", pages)[0] == "PO"


def test_a_waiver_of_rule_26_1_does_not_make_it_an_mgo():
    """清洗豁免公告满篇都是 26.1，说的却是这单**不必**做强制要约。"""
    pages = {1: "本公司將向執行人員申請豁免根據規則26.1提出強制性全面要約的責任。"}
    assert extractor.extract_offer_type("公告 有關清洗豁免", pages)[0] != "MGO"


# ------------------------------------------- 类型＝两个维度合成，不是三个并列类别

def test_the_two_dimensions_are_reported_separately():
    """底层存维度、输出层合成标签 —— 换口径能重切，不用改判定逻辑。"""
    got = extractor.classify_offer("聯合公告 自願無條件全面現金要約", {})
    assert (got.obligation, got.scope, got.label) == ("voluntary", "full", "VGO")


def test_voluntary_plus_full_is_a_vgo_even_when_unconditional():
    """03389 亨得利：标题白纸黑字「自願無條件全面現金要約」。

    做得成「无条件」说明要约人已持多数、規則26.1 根本没触发 ——
    「無條件」讲的是条件，不是义务基础，不能拿它去判 MGO。
    """
    title = "聯合公告 自願無條件全面現金要約 及 恢復買賣"
    assert extractor.classify_offer(title, {}).label == "VGO"


def test_partial_scope_terminates_the_judgement():
    """范围维度优先：只收固定数量就是 PO，正文里的「強制性」不再翻盘。

    ⚠️ 这条和上一版**相反**。上一版是「強制性压过部分」，依据是
    01980/01796/02362 三单答案记 MGO；口径复核后确认那三单应记 PO ——
    强制要约必须全面（規則26 要求就全部股份提出），所以部分要约在
    义务上必然是自愿的，两个词一起出现时说了算的是范围那一维。
    """
    pages = {1: "要約人須根據收購守則規則26.1提出要約。本公司宣佈自願現金"
                "部分收購要約，收購最多755,300,000股要約股份。"}
    got = extractor.classify_offer("聯合公告 及 恢復買賣", pages)
    assert got.scope == "partial"
    assert got.obligation == "voluntary", "部分要约在义务上一定是自愿的"
    assert got.label == "PO"


def test_a_partial_offer_in_the_body_beats_rule_26_1():
    """同上，但换成 09638 那种标题不写、正文才写的。"""
    pages = {1: "釋義：「收購守則」指公司收購及合併守則，包括規則26.1。",
             2: "提出附帶先決條件的自願現金部份收購要約。"}
    assert extractor.classify_offer("公告", pages).label == "PO"


def test_a_stray_partial_that_is_not_a_partial_offer_does_not_flip_it():
    """「部分股東之不可撤銷承諾」里的「部分」不是范围维度的证据。"""
    title = "聯合公告 強制性無條件現金要約 及 部分股東之不可撤銷承諾"
    got = extractor.classify_offer(title, {})
    assert got.scope != "partial" and got.label == "MGO"


def test_mandatory_implies_full_scope():
    """規則26 的强制要约必须就全部股份提出 —— 范围不用另找证据。"""
    got = extractor.classify_offer("聯合公告 強制性無條件現金要約", {})
    assert (got.obligation, got.scope, got.label) == ("mandatory", "full", "MGO")


def test_a_whitewash_waiver_is_still_not_a_mandatory_offer():
    title = "公告 (1) 自願現金部分收購要約 及 (2) 申請豁免須提出強制性全面要約的責任"
    assert extractor.classify_offer(title, {}).label == "PO"

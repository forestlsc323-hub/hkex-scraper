from hkex_offers import config, extract


def primary(text):
    return extract.pick_primary(extract.find_hits(text))


def test_chinese_premium_over_last_trading_day():
    text = ("要約價每股要約股份3.50港元，較股份於最後交易日在聯交所所報"
            "收市價每股2.80港元溢價約25.00%。")
    hit = primary(text)
    assert hit.kind == extract.PREMIUM
    assert hit.pct == 25.00
    assert hit.benchmark == "last_trading_day_close"


def test_chinese_discount():
    text = "要約價較股份於最後交易日之收市價每股1.00港元折讓約12.5%。"
    hit = primary(text)
    assert hit.kind == extract.DISCOUNT
    assert hit.pct == 12.5


def test_english_premium():
    text = ("The Offer Price represents a premium of approximately 20.51% over the "
            "closing price of HK$1.95 per Share as quoted on the Stock Exchange on "
            "the Last Trading Day.")
    hit = primary(text)
    assert hit.kind == extract.PREMIUM
    assert hit.pct == 20.51
    assert hit.benchmark == "last_trading_day_close"


def test_english_reversed_order():
    text = "representing a 8.7% discount to the average closing price."
    hit = primary(text)
    assert hit.kind == extract.DISCOUNT
    assert hit.pct == 8.7


def test_last_trading_day_wins_over_averages():
    text = ("溢價約5.0%，較股份於最後十個交易日之平均收市價。"
            "此外，較股份於最後交易日之收市價溢價約18.0%。")
    hit = primary(text)
    assert hit.pct == 18.0
    assert hit.benchmark == "last_trading_day_close"


def test_nav_comparison_is_deprioritised():
    text = "較每股資產淨值折讓約60.0%。要約價較最後交易日收市價溢價約3.0%。"
    hit = primary(text)
    assert hit.pct == 3.0


def test_no_percentage_returns_none():
    assert primary("本公告並無提及任何溢價。") is None


def test_offer_type_classification():
    assert config.classify_offer_type("強制性無條件現金要約") == config.MGO
    assert config.classify_offer_type("自願有條件現金要約") == config.VGO
    assert config.classify_offer_type("有關收購事項之部分要約") == config.PO
    assert config.classify_offer_type("MANDATORY UNCONDITIONAL CASH OFFER") == config.MGO
    assert config.classify_offer_type("VOLUNTARY CONDITIONAL CASH OFFER") == config.VGO
    assert config.classify_offer_type("PARTIAL OFFER BY XYZ") == config.PO
    assert config.classify_offer_type("董事會會議日期") is None


def test_partial_beats_mandatory_when_both_present():
    assert config.classify_offer_type("強制性有條件部分要約") == config.PO

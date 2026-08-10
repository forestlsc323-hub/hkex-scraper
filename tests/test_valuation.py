"""估值组的测试。

这一层是铁律一的落点：抽取层一个数都不算，乘除全在这里，
每个结果都能说出「等于哪两个带出处的数字算出来的」。
"""

from __future__ import annotations

from decimal import Decimal

from hkexdb import valuation as V


# ---------------------------------------------------------------- 规模 vs 估值

def test_implied_equity_value_is_not_the_deal_size():
    """「全部已發行股本估值」不是垃圾，只是不能当 deal size。

    判别口诀：这笔钱付给谁？付给接纳要约的公众股东的才是要约规模；
    把整家公司作价多少，那是估值指标，做倍数时才用。
    两个都要，但绝不能混 —— 所以它们是两列。
    """
    d = V.derive(offer_price="2.20", total_shares="1200008445",
                 nav_per_share="", undisturbed_spot="", last_trading_spot="")
    assert d.implied_equity_value == "2640018579.00"
    assert "1200008445 股 × 2.20 港元" in d.implied_equity_basis


def test_pb_uses_nav_which_is_deliberately_excluded_from_the_premium():
    """NAV 不做溢价率主值，但必须单独存 —— 壳股估值看 P/B 不看 P/E。"""
    d = V.derive(offer_price="2.20", total_shares="", nav_per_share="3.73",
                 undisturbed_spot="", last_trading_spot="")
    assert d.pb_ratio == "0.590"
    assert "每股净资产 3.73" in d.pb_basis


def test_runup_is_the_leak_evidence_that_decides_the_basis():
    """不受干扰日→最后交易日跑了多少，就是消息漏没漏出去的证据。

    3336 实测跑了 27.36% —— 最后交易日收市价已经把要约预期计进去了，
    拿它当基准会系统性低估溢价。这正是该用哪个基准的判据。
    """
    d = V.derive(offer_price="2.20", total_shares="", nav_per_share="",
                 undisturbed_spot="3.18", last_trading_spot="4.05")
    assert d.runup_pct == "27.36"
    assert "4.05" in d.runup_basis and "3.18" in d.runup_basis


def test_missing_inputs_leave_the_field_blank_not_zero():
    """缺料就留空，绝不用别的数顶上，更不能填 0 —— 0 会被当成真值。"""
    d = V.derive(offer_price="", total_shares="100", nav_per_share="1",
                 undisturbed_spot="1", last_trading_spot="2")
    assert d.implied_equity_value == "" and d.pb_ratio == ""
    assert d.runup_pct == "27.36" or d.runup_pct == "100.00"


def test_zero_nav_does_not_blow_up():
    """资不抵债的公司 NAV 可能是 0 或负 —— 不能让它把整份公告搞崩。"""
    d = V.derive(offer_price="1.0", total_shares="", nav_per_share="0",
                 undisturbed_spot="", last_trading_spot="")
    assert d.pb_ratio == ""


def test_garbage_input_is_survived():
    d = V.derive(offer_price="待补", total_shares="—", nav_per_share="n/a",
                 undisturbed_spot="", last_trading_spot="")
    assert d == V.Derived()


# ---------------------------------------------------------------- 交易性质

def test_deep_discount_reads_as_shell_or_technical():
    """要约有两个物种。混在一起算中位数＝废数据 ——
    1417 折让 55.57%，那不是在给控制权定价。"""
    g = V.guess_nature(premium_pct="-55.57", listing_intent="",
                       debt_conversion=False, offer_type="MGO")
    assert "买壳" in g.label and "待确认" in g.label
    assert any("深折让" in r for r in g.reasons)


def test_positive_premium_reads_as_a_real_acquisition():
    g = V.guess_nature(premium_pct="4.49", listing_intent="",
                       debt_conversion=False, offer_type="PO")
    assert "产业收购" in g.label


def test_delisting_intent_wins_over_the_premium():
    """私有化是按意图定的，不是按溢价定的。"""
    g = V.guess_nature(premium_pct="-55", listing_intent="拟撤销上市",
                       debt_conversion=False, offer_type="MGO")
    assert g.label.startswith("私有化")


def test_debt_conversion_plus_discount_reads_as_technical():
    """债转股被动触发 26.1，要约价＝换股价，走程序保上市地位。"""
    g = V.guess_nature(premium_pct="-5", listing_intent="",
                       debt_conversion=True, offer_type="MGO")
    assert g.label.startswith("技术性要约")


def test_every_guess_carries_its_reasons():
    """铁律二：分类错了是静默污染。所以永远要说出依据。"""
    for kwargs in [dict(premium_pct="-55.57", listing_intent="", debt_conversion=False),
                   dict(premium_pct="4.49", listing_intent="", debt_conversion=False),
                   dict(premium_pct="-5", listing_intent="", debt_conversion=True)]:
        g = V.guess_nature(offer_type="MGO", **kwargs)
        assert g.reasons, f"给了结论却说不出依据：{g.label}"


def test_every_guess_is_marked_as_needing_confirmation():
    """这一层永远不是定论。表里出现一个笃定的分类比留空更危险。"""
    g = V.guess_nature(premium_pct="4.49", listing_intent="",
                       debt_conversion=False, offer_type="PO")
    assert "待确认" in g.label


def test_unclassifiable_says_so_instead_of_going_blank():
    """空着会让人以为「没这回事」，实际是「判不出来」。这两件事不一样。"""
    g = V.guess_nature(premium_pct="-15.45", listing_intent="",
                       debt_conversion=False, offer_type="VGO")
    assert "未分类" in g.label


def test_a_big_runup_is_flagged_even_when_it_does_not_change_the_label():
    g = V.guess_nature(premium_pct="-15.45", listing_intent="",
                       debt_conversion=False, offer_type="VGO",
                       runup_pct="27.36")
    assert any("慎用最后交易日口径" in r for r in g.reasons)


def test_the_discount_threshold_is_tunable_not_hardcoded():
    """阈值是启发式，不是真理。放出来让人改，改了重跑，不手改结果。"""
    g = V.guess_nature(premium_pct="-20", listing_intent="",
                       debt_conversion=False, offer_type="MGO",
                       deep_discount_pct=Decimal("-15"))
    assert "买壳" in g.label

"""主值选取规则的验收：三单人工答案必须被规则原样复现。

这是本项目第一次有「人工标准答案」可对。铁律三要的逐字段准确率报告，
就从这三单起步（目标是 31 单）。

⚠️ 规则本身是从这三单反推的，不是附录 A。三单一致不代表规则正确，
附录 A 到手后必须重跑。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hkexdb import selectors

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.yaml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(params=FIXTURES, ids=lambda p: p.stem)
def fx(request):
    return load(request.param)


# ---------------------------------------------------------------- 溢价率

def test_premium_rule_reproduces_human_answer(fx):
    """30 日均价 + 未受干扰日优先 —— 三单答案必须一字不差地复现。"""
    pick = selectors.select_primary_premium(fx["price_comparisons"])
    assert pick is not None, "选不出主值溢价率"
    assert pick.signed_pct == Decimal(fx["human_answer"]["premium_pct"]), (
        f"规则选出 {pick.signed_pct}%（{pick.label}），"
        f"人工答案 {fx['human_answer']['premium_pct']}%（{fx['human_answer']['premium_basis']}）")
    assert pick.window == "30d"


def test_premium_pick_carries_provenance(fx):
    """铁律三：选出来的值必须带页码和原文。"""
    pick = selectors.select_primary_premium(fx["price_comparisons"])
    assert pick.page > 0
    assert pick.source_quote.strip()
    assert str(pick.signed_pct).lstrip("-") in pick.source_quote.replace(",", "")


def test_undisturbed_anchor_beats_last_trading_day_on_3336():
    """3336 是唯一同时有两套锚点的单 —— 规则必须挑未受干扰日那条。

    若挑成最後交易日，答案会是 −17.50%，与人工答案 −15.45% 差 2.05pp。
    """
    fx = load(FIXTURE_DIR / "3336_vgo_20260518.yaml")
    pick = selectors.select_primary_premium(fx["price_comparisons"])

    assert pick.anchor == "undisturbed"
    assert pick.signed_pct == Decimal("-15.45")
    assert "最後交易日前30日均价" in pick.rejected      # 被挤掉的那条

    wrong = next(c for c in fx["price_comparisons"]
                 if c["anchor"] == "last_trading_day" and c["window"] == "30d")
    assert Decimal(wrong["stated_pct"]) == Decimal("17.50")


def test_single_anchor_deals_fall_back_to_last_trading_day():
    """1417 与 00195 没有未受干扰日，规则应落到最後交易日锚点。"""
    for name, expected in (("1417_mgo_20260615", Decimal("-55.57")),
                           ("00195_po_20260615", Decimal("4.49"))):
        fx = load(FIXTURE_DIR / f"{name}.yaml")
        anchors = {c["anchor"] for c in fx["price_comparisons"]}
        assert "undisturbed" not in anchors

        pick = selectors.select_primary_premium(fx["price_comparisons"])
        assert pick.anchor == "last_trading_day"
        assert pick.signed_pct == expected


def test_no_thirty_day_falls_back_but_says_which_window_it_used():
    """没有 30 日均价时退到更短的窗口 —— 但**口径必须跟着变**。

    ⚠️ 这条规则改过一次。原来是「宁可不给答案」，理由是怕把一个
    「对收市价」的数悄悄写进「30 日均价」字段。那个顾虑是对的，但
    解法用错了：只要口径和数字同列显示，退档就不是静默降级。
    改的依据是你答案表里两单（都是公告本身没印 30 日）：
        01833 平安好醫生 -4.23% ← 前10日均价
        01980 天鴿互動   +2.10% ← 前5日均价
    留空的做法把这两单算成「抽不出」，而它们其实抽得出。
    """
    only_spot = [{"label": "收市价", "anchor": "last_trading_day", "window": "spot",
                  "stated_pct": "10.00", "stated_direction": "premium",
                  "page": 1, "quote": "x"}]
    pick = selectors.select_primary_premium(only_spot)
    assert pick.signed_pct == Decimal("10.00")
    assert pick.window == "spot" and pick.label == "收市价"
    assert pick.fell_back


def test_the_fallback_ladder_prefers_the_longest_window_available():
    def item(window, pct):
        return {"label": window, "anchor": "last_trading_day", "window": window,
                "stated_pct": pct, "stated_direction": "premium",
                "page": 1, "quote": "x"}

    both = [item("spot", "1"), item("5d", "2"), item("10d", "3")]
    assert selectors.select_primary_premium(both).window == "10d"
    assert selectors.select_primary_premium(both[:2]).window == "5d"


def test_the_fallback_can_be_switched_off():
    """关掉退档就回到老行为：只认 30 日，别的一律留空。"""
    only_spot = [{"label": "收市价", "anchor": "last_trading_day", "window": "spot",
                  "stated_pct": "10.00", "stated_direction": "premium",
                  "page": 1, "quote": "x"}]
    assert selectors.select_primary_premium(only_spot, fallback=()) is None


def test_a_thirty_day_item_is_never_displaced_by_the_fallback():
    """有 30 日就必须用 30 日 —— 退档只在缺口时生效。"""
    rows = [{"label": "收市价", "anchor": "last_trading_day", "window": "spot",
             "stated_pct": "1", "stated_direction": "premium", "page": 1, "quote": "x"},
            {"label": "前30日均价", "anchor": "last_trading_day", "window": "30d",
             "stated_pct": "2", "stated_direction": "premium", "page": 1, "quote": "x"}]
    pick = selectors.select_primary_premium(rows)
    assert pick.window == "30d" and not pick.fell_back


def test_the_other_anchor_in_the_same_window_is_carried_along():
    """08439 / 3336 那个争议：选了一个，另一个也要能看见。"""
    rows = [{"label": "未受干扰日前30日均价", "anchor": "undisturbed", "window": "30d",
             "stated_pct": "129.8", "stated_direction": "premium",
             "page": 1, "quote": "x"},
            {"label": "最后交易日前30日均价", "anchor": "last_trading_day",
             "window": "30d", "stated_pct": "40.8", "stated_direction": "premium",
             "page": 1, "quote": "x"}]
    pick = selectors.select_primary_premium(rows)
    assert pick.anchor == "undisturbed"
    assert pick.other_anchor == "最后交易日前30日均价" and pick.other_pct == "40.8"


def test_flipping_the_anchor_priority_flips_the_pick():
    """口径之争是配置项，不是常量 —— 改 config.yaml 就能翻过来。"""
    rows = [{"label": "未受干扰日前30日均价", "anchor": "undisturbed", "window": "30d",
             "stated_pct": "129.8", "stated_direction": "premium",
             "page": 1, "quote": "x"},
            {"label": "最后交易日前30日均价", "anchor": "last_trading_day",
             "window": "30d", "stated_pct": "40.8", "stated_direction": "premium",
             "page": 1, "quote": "x"}]
    pick = selectors.select_primary_premium(
        rows, anchor_priority=("last_trading_day", "undisturbed", "pre_rule37"))
    assert pick.signed_pct == Decimal("40.8")
    assert pick.other_anchor == "未受干扰日前30日均价"


def test_unknown_anchor_is_not_silently_accepted():
    unknown = [{"label": "对某未知基准", "anchor": "some_new_anchor", "window": "30d",
                "stated_pct": "10.00", "stated_direction": "premium",
                "page": 1, "quote": "x"}]
    assert selectors.select_primary_premium(unknown) is None


# ---------------------------------------------------------------- 交易规模

# 交易规模的选取规则和它的回归测试都在抽取层（tests/test_real_wording.py
# 里「这笔钱付给谁」那几条）—— 这里原来那两个测试用的是 selectors 里的
# 第二套实现，而流水线从来没调用过它，测了个寂寞。


def test_1417_printed_vs_recomputed_gap_is_recorded():
    """三单中唯一的口径分歧，必须留在 fixture 里等你拍板，不许悄悄抹平。

    公告印「約5,440萬」= 54,400,000；104,830,000 × 0.519 = 54,406,770。
    差 6,770（0.0124%）。审计级数据库里这个差额要有明确归属。
    """
    fx = load(FIXTURE_DIR / "1417_mgo_20260615.yaml")
    cand = next(c for c in fx["deal_size_candidates"] if c["key"] == "offer_max_cash")

    assert cand["printed_is_rounded"] is True
    assert cand["value"] == 54_400_000
    assert cand["value_recomputed"] == 54_406_770
    assert cand["value_recomputed"] - cand["value"] == 6_770
    assert Decimal(104_830_000) * Decimal("0.519") == Decimal(cand["value_recomputed"])


def test_other_two_deals_have_no_printed_recomputed_gap():
    """对照组：另两单公告印的就是精确值，不存在这个分歧。"""
    for name in ("3336_vgo_20260518", "00195_po_20260615"):
        fx = load(FIXTURE_DIR / f"{name}.yaml")
        cand = next(c for c in fx["deal_size_candidates"]
                    if c["key"] == "offer_max_cash")
        assert cand["printed_is_rounded"] is False
        assert Decimal(str(cand["value"])) == Decimal(str(cand["value_recomputed"]))


# ---------------------------------------------------------------- 类型与日期

def test_offer_type_and_date_match_human_answer(fx):
    assert fx["deal"]["offer_type"] == fx["human_answer"]["offer_type"]
    assert fx["deal"]["first_announcement_date"] == \
        fx["human_answer"]["first_announcement_date"]


def test_all_three_offer_types_are_covered():
    types = {load(p)["deal"]["offer_type"] for p in FIXTURES}
    assert types == {"MGO", "VGO", "PO"}


def test_field_accuracy_report_is_three_for_three(capsys):
    """铁律三要求的逐字段准确率报告 —— 目前 3 单，目标 31 单。"""
    fields = ("first_announcement_date", "offer_type", "premium_pct", "deal_size")
    tally = {f: [0, 0] for f in fields}

    for path in FIXTURES:
        fx = load(path)
        human = fx["human_answer"]
        got = {
            "first_announcement_date": fx["deal"]["first_announcement_date"],
            "offer_type": fx["deal"]["offer_type"],
            "premium_pct": str(selectors.select_primary_premium(
                fx["price_comparisons"]).signed_pct),
            # 交易规模按 fixture 里标好的口径取（offer_max_cash）——
            # 这一栏的**规则**由抽取层的真实措辞测试守着，这里只是
            # 三单固定样本的对答案报告。
            "deal_size": str(next(
                c["value"] for c in fx["deal_size_candidates"]
                if c["key"] == "offer_max_cash")),
        }
        for f in fields:
            tally[f][1] += 1
            if str(got[f]) == str(human[f]):
                tally[f][0] += 1

    lines = ["", "逐字段准确率（样本 %d 单）" % len(FIXTURES)]
    for f in fields:
        ok, total = tally[f]
        lines.append(f"  {f:<26} {ok}/{total}  {ok / total * 100:5.1f}%")
    print("\n".join(lines))

    for f in fields:
        assert tally[f][0] == tally[f][1], f"{f} 未全对：{tally[f]}"


# ------------------------------------------------- 主值方向的算术复核（V5 上桌）

def _pick(benchmark: str, stated_pct: str, direction: str):
    """构造一个主值选取结果。⚠️ 必须走 select_primary_premium，
    不能手搓 PremiumPick —— 手搓的话字段少一个也测不出来。"""
    return selectors.select_primary_premium([{
        "anchor": "last_trading_day", "window": "30d",
        "label": "最后交易日前30日均价", "stated_pct": stated_pct,
        "stated_direction": direction, "benchmark": benchmark,
        "page": 3, "quote": "较…折让约…%"}])


def test_the_pick_carries_the_numbers_needed_to_recheck_it():
    pick = _pick("1.00", "20.00", "discount")
    assert pick.benchmark == "1.00" and pick.stated_pct == "20.00"


def test_wording_and_arithmetic_agreeing_is_a_pass():
    check = selectors.check_direction(_pick("1.00", "20.00", "discount"), "0.80")
    assert check.agrees and check.pct_agrees


def test_a_premium_written_as_a_discount_is_caught():
    """要约价高于基准却写「折让」—— 光看百分比看不出来，必须比大小。"""
    check = selectors.check_direction(_pick("1.00", "20.00", "discount"), "1.20")
    assert not check.agrees
    assert check.arithmetic == "premium"
    assert check.pct_agrees          # 数字自洽，错的只是那两个字


def test_a_mismatched_benchmark_shows_up_as_the_percentage_not_reconciling():
    """基准价配错行时（09929 那种 PDF 文字层错位），方向可能碰巧还对，
    但百分比一定复算不出来 —— 所以 pct_agrees 才是那一层的探针。"""
    check = selectors.check_direction(_pick("220.00", "20.00", "discount"), "0.11")
    assert check.agrees          # 0.11 < 220，算术上确实是折让
    assert not check.pct_agrees  # 但折让 99.95%，不是公告印的 20%


def test_rounding_does_not_count_as_a_disagreement():
    # 0.80 / 1.005 = 20.398% 折让，公告印 20.40%
    check = selectors.check_direction(_pick("1.005", "20.40", "discount"), "0.80")
    assert check.agrees and check.pct_agrees


def test_no_offer_price_means_no_opinion():
    assert selectors.check_direction(_pick("1.00", "20.00", "discount"), "") is None


def test_no_benchmark_means_no_opinion():
    assert selectors.check_direction(_pick("", "20.00", "discount"), "0.80") is None


def test_a_zero_benchmark_never_divides():
    assert selectors.check_direction(_pick("0", "20.00", "discount"), "0.80") is None


def test_nothing_picked_means_nothing_to_check():
    assert selectors.check_direction(None, "0.80") is None

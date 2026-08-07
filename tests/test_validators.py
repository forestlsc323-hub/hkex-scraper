"""把两单真实公告跑成回归测试。

铁律一在这里兑现：fixture 里只有从 PDF 摘出来的原文数字，
所有百分比、乘积、加总由 validators.py 复算。
将来改抽取逻辑或改校验阈值，这些测试会立刻告诉你有没有改坏。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from hkexdb import validators as V

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = sorted(FIXTURE_DIR.glob("*.yaml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def comparisons_of(fx: dict) -> list[V.PriceComparison]:
    return [
        V.PriceComparison(
            label=c["label"],
            benchmark=Decimal(c["benchmark"]),
            benchmark_decimals=c["benchmark_decimals"],
            benchmark_is_exact=c["benchmark_is_exact"],
            stated_pct=Decimal(c["stated_pct"]),
            stated_direction=c["stated_direction"],
            page=c["page"],
            source_quote=c["quote"],
        )
        for c in fx["price_comparisons"]
    ]


@pytest.fixture(params=FIXTURES, ids=lambda p: p.stem)
def fx(request):
    return load(request.param)


# ---------------------------------------------------------------- 铁律三

def test_every_field_carries_a_source_quote(fx):
    """铁律三：无出处的字段不许存在。"""
    for cmp_ in fx["price_comparisons"]:
        assert cmp_["quote"].strip(), f"{cmp_['label']} 缺原文"
        assert isinstance(cmp_["page"], int) and cmp_["page"] > 0

    for key in ("offer_price_evidence", "offer_type_evidence",
                "first_announcement_evidence"):
        ev = fx["deal"][key]
        assert ev["quote"].strip() and isinstance(ev["page"], int)

    assert fx["six_month_range"]["quote"].strip()


def test_primary_deal_size_stays_null_until_rules_arrive(fx):
    """附录 A 未到手，主值口径未定 —— 不许有人偷偷填一个上去。"""
    assert fx["deal_size_primary"] is None
    assert fx["meta"]["rules_version"] is None


# ---------------------------------------------------------------- V4/V5/V6

def test_v4_v5_v6_all_pass(fx):
    findings = V.run_price_comparisons(
        Decimal(fx["deal"]["offer_price"]),
        comparisons_of(fx),
        Decimal(fx["six_month_range"]["low"]),
        Decimal(fx["six_month_range"]["high"]),
        nonmarket_labels=frozenset(fx.get("nonmarket_labels", [])),
    )
    failures = [str(f) for f in findings if not f.passed]
    assert not failures, "\n" + "\n".join(failures)

    expected = fx["expected_findings"]
    for code, key in (("V4", "v4_all_pass"), ("V5", "v5_all_pass"),
                      ("V6", "v6_all_pass")):
        got = all(f.passed for f in findings if f.code == code)
        assert got == expected[key]


def test_equality_check_would_false_flag_the_vgo():
    """本项目最重要的一条经验，用真实数据钉死。

    3336 那单把均价印成「約 X.XX」，公告百分比是用未取整的真值算的。
    若 V4 写成等式检验，11 项里有 8 项会被误报为错误 ——
    真错误（附录 D-1 型）就会淹没在假警报里。
    """
    fx = load(FIXTURE_DIR / "3336_vgo_20260518.yaml")
    offer = Decimal(fx["deal"]["offer_price"])

    false_flags = 0
    for cmp_ in comparisons_of(fx):
        stated = (cmp_.stated_pct if cmp_.stated_direction == V.PREMIUM
                  else -cmp_.stated_pct)
        point = V.signed_pct(offer, cmp_.benchmark)      # 天真的等式检验
        naive_fail = abs(point - stated) > Decimal("0.01")
        interval_pass = V.v4_percentage_recompute(offer, cmp_).passed
        if naive_fail and interval_pass:
            false_flags += 1

    assert false_flags == fx["expected_findings"]["v4_would_fail_under_equality_check"]
    assert false_flags == 8


def test_exact_benchmarks_reconcile_to_two_decimals():
    """对照组：1417 那单基准是精确值，点估计就该吻合。"""
    fx = load(FIXTURE_DIR / "1417_mgo_20260615.yaml")
    offer = Decimal(fx["deal"]["offer_price"])
    for cmp_ in comparisons_of(fx):
        if not cmp_.benchmark_is_exact:
            continue
        stated = (cmp_.stated_pct if cmp_.stated_direction == V.PREMIUM
                  else -cmp_.stated_pct)
        assert abs(V.signed_pct(offer, cmp_.benchmark) - stated) <= Decimal("0.01")


# ---------------------------------------------------------------- V3

def test_v3_closure_1417():
    n = load(FIXTURE_DIR / "1417_mgo_20260615.yaml")["numbers"]
    D = Decimal

    f = V.v3_closure("股数：一致行动人 + 要约股份 = 已发行",
                     [("一致行动人", D(n["concert_party_shares"])),
                      ("要约股份", D(n["offer_shares"]))],
                     D(n["total_issued_shares"]))
    assert f.passed, f

    f = V.v3_closure("SPA1 + SPA2 股数 = 一致行动人持股",
                     [("SPA1", D(n["spa1_shares"])), ("SPA2", D(n["spa2_shares"]))],
                     D(n["concert_party_shares"]))
    assert f.passed, f

    f = V.v3_closure("SPA1 + SPA2 代价 = 出售股份代价总额",
                     [("SPA1", D(n["spa1_consideration"])),
                      ("SPA2", D(n["spa2_consideration"]))],
                     D(n["spa_total_consideration"]))
    assert f.passed, f


def test_1417_offer_price_rounds_up_from_spa_price():
    """要约价 0.519 略高于 SPA 实付 0.518519 —— 差额必须能被完全解释。

    符合收购守则「要约价不得低于已付最高价」。这个 144,527 港元的缺口
    不是错误，但必须能算得出来，否则就是 V3 失败。
    """
    n = load(FIXTURE_DIR / "1417_mgo_20260615.yaml")["numbers"]
    D = Decimal
    offer = D("0.519")
    spa_price = D(n["spa_total_consideration"]) / D(n["concert_party_shares"])

    assert offer > spa_price
    gap = D(n["total_issued_shares"]) * offer - (
        D(n["spa_total_consideration"]) + D(n["offer_shares"]) * offer)
    explained = D(n["concert_party_shares"]) * (offer - spa_price)
    assert abs(gap - explained) < D("0.01")
    assert round(gap) == 144527


def test_v3_closure_3336():
    n = load(FIXTURE_DIR / "3336_vgo_20260518.yaml")["numbers"]
    D = Decimal
    offer = D("2.20")

    f = V.v3_closure("待售股份 + 要约股份 = 已发行",
                     [("待售", D(n["sale_shares"])),
                      ("要约", D(n["offer_shares"]))],
                     D(n["total_issued_shares"]))
    assert f.passed, f

    # 对价 = 股数 × 要约价，这单是精确整除，容差 0
    assert D(n["sale_shares"]) * offer == D(str(n["sale_consideration"]))
    assert D(n["offer_shares"]) * offer == D(str(n["offer_max_cash"]))

    f = V.v3_closure("购股协议对价 + 要约最高现金 = 100% 股本隐含估值",
                     [("购股协议", D(str(n["sale_consideration"]))),
                      ("要约", D(str(n["offer_max_cash"])))],
                     D(n["total_issued_shares"]) * offer)
    assert f.passed, f


def test_3336_nav_per_share_reconciles():
    n = load(FIXTURE_DIR / "3336_vgo_20260518.yaml")["numbers"]
    per_share = Decimal(n["audited_nav_total"]) / Decimal(n["total_issued_shares"])
    assert abs(per_share - Decimal("3.73")) < Decimal("0.005")   # 公告写「約3.73」


def test_3336_undertaking_scenarios_bound_the_committed_shares():
    """附录 D-4：多情境要算出区间，不能按章节标题挑一个。"""
    n = load(FIXTURE_DIR / "3336_vgo_20260518.yaml")["numbers"]
    trustee = n["share_award_trustee_shares"]
    committed_max = trustee                          # 一股奖励都不授出
    committed_min = trustee - n["max_new_awards"]    # 足额授出 72,000,000
    assert committed_max == 354_345_774
    assert committed_min == 282_345_774
    assert committed_min < committed_max


# ---------------------------------------------------------------- V8

def test_v8_passes_on_both_real_deals(fx):
    values = [(c["label"], Decimal(c["benchmark"])) for c in fx["price_comparisons"]]
    values += [("要约价", Decimal(fx["deal"]["offer_price"])),
               ("六个月低", Decimal(fx["six_month_range"]["low"])),
               ("六个月高", Decimal(fx["six_month_range"]["high"]))]
    f = V.v8_magnitude(fx["meta"]["fixture_id"], values)
    assert f.passed == fx["expected_findings"]["v8_all_pass"], f


# ---------------------------------------------------------------- 附录 D 回归

def test_appendix_d1_unit_typo_is_caught():
    """D-1：均价印成 63.7 港元（实为 63.7 港仙），要约价 0.167。

    V6 靠「超出六个月高低价区间」抓；V8 靠「每股数量级差 381 倍」抓。
    两条都要响，才说明校验器没有单点失效。
    """
    bad = V.PriceComparison(
        label="30日均价", benchmark=Decimal("63.7"), benchmark_decimals=1,
        benchmark_is_exact=False, stated_pct=Decimal("0.460"),
        stated_direction=V.DISCOUNT, page=1, source_quote="（附录 D-1）",
    )
    offer = Decimal("0.167")

    v6 = V.v6_benchmark_in_range(bad, Decimal("0.10"), Decimal("0.70"))
    assert not v6.passed, "V6 应当发现均价超出六个月区间"

    v8 = V.v8_magnitude("D-1", [("均价", Decimal("63.7")), ("要约价", offer),
                                ("六个月高", Decimal("0.70"))])
    assert not v8.passed, "V8 应当发现每股数量级异常"

    v4 = V.v4_percentage_recompute(offer, bad)
    assert not v4.passed, "V4 应当发现 0.460% 与真实折让 73.8% 不符"

    真实折让 = (1 - offer / Decimal("0.637")) * 100
    assert abs(真实折让 - Decimal("73.8")) < Decimal("0.1")


def test_appendix_d2_direction_flip_is_caught():
    """D-2：30日均价 0.243、要约价 0.276，公告却写「折讓約13.58%」。

    数值对、方向错 —— 只有比大小才抓得到，复算百分比抓不到。
    """
    flipped = V.PriceComparison(
        label="30日均价", benchmark=Decimal("0.243"), benchmark_decimals=3,
        benchmark_is_exact=True, stated_pct=Decimal("13.58"),
        stated_direction=V.DISCOUNT, page=1, source_quote="（附录 D-2）",
    )
    offer = Decimal("0.276")

    assert not V.v5_direction(offer, flipped).passed, "V5 应当发现方向标反"

    # 关键点：绝对值是对的，所以「只复算数值」的校验会放它过去
    assert abs(abs(V.signed_pct(offer, flipped.benchmark))
               - Decimal("13.58")) < Decimal("0.01")


def test_appendix_d4_max_across_scenarios():
    """D-4：购股权全部行使后总代价反而更低，不得按章节标题选，须取最大。"""
    scenarios = {"购股权不行使": Decimal("118788430.05"),
                 "购股权全部行使": Decimal("117411504")}
    assert max(scenarios.values()) == Decimal("118788430.05")
    assert scenarios["购股权全部行使"] < scenarios["购股权不行使"]


def test_appendix_d5_nominal_vs_actual_gap():
    """D-5：要约价值 187,727,500（含承诺股份）vs 财务资源 111,744,375（剔除后）。

    对照：3336 那单也有不可撤销承诺，但受托人承诺的是「接纳」，
    要约人仍需付现，故无缺口 —— 两者不可混为一谈。
    """
    nominal, actual = Decimal("187727500"), Decimal("111744375")
    assert nominal > actual

    fx = load(FIXTURE_DIR / "3336_vgo_20260518.yaml")
    assert fx["irrevocable_undertakings"]["exists"] is True
    assert fx["irrevocable_undertakings"]["reduces_payable"] is False
    n = fx["numbers"]
    assert Decimal(str(n["offer_max_cash"])) == \
        Decimal(n["offer_shares"]) * Decimal("2.20")     # 无缺口

"""校验器：用公告自己的数字，交叉验证公告自己的百分比。

**铁律一在这里落地。** 模型只负责把数字和原文摘出来，
这个模块里的每一次加减乘除都是 Python 做的，可复现、可回归。

⚠️ 关于编号：附录 D 引用了 V3~V11，但只给了「检测点」名称，
没给判定条件和阈值（推测在尚未提供的附录 A 里）。
下面的 V3/V4/V5/V6/V8 是按附录 D 的描述**临时实现**的，
附录 A 到手后必须逐条核对、以附录 A 为准。
V7/V9/V10/V11 依赖附录 A 的定义，此处未实现。

## 为什么 V4 是区间检验而不是等式检验

这是从真实公告里学到的，不是设计洁癖。

浦江中国（1417）那单把基准价印成精确值，用印出来的数复算，
六项全部吻合到小数点后两位。

蓝思／巨腾（3336）那单把均价印成「約 X.XX」——两位小数。
公告里的百分比是用**未取整的真实均价**算的，
所以拿「印出来的 2 位小数」去复算，11 项里有 8 项对不上，
最大偏差 0.32 个百分点。

如果 V4 写成等式检验，这 8 项会被报成错误，
而真正的错误（附录 D-1 那种）会淹没在假警报里 —— 校验器就废了。

正确做法：印成 X.XX 意味着真值落在 [X-0.005, X+0.005]，
把这个区间映射成百分比区间，再看公告写的百分比在不在里面。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

PREMIUM = "premium"
DISCOUNT = "discount"

# 公告里的百分比自身也是四舍五入到 2 位小数的，检验时要放这么多
_PCT_ROUNDING = Decimal("0.005")


@dataclass(frozen=True)
class Finding:
    """一条校验结果。detail 要写得让人不看代码也能复核。"""

    code: str            # V3 / V4 / ...
    passed: bool
    subject: str         # 检验对象
    detail: str

    def __str__(self) -> str:
        return f"{self.code} {'PASS' if self.passed else 'FAIL'} | {self.subject} | {self.detail}"


@dataclass(frozen=True)
class PriceComparison:
    """公告「价值比较」一节里的一项。字段全部照抄原文，不做换算。"""

    label: str
    benchmark: Decimal          # 公告印出来的基准价
    benchmark_decimals: int     # 印了几位小数；配合 is_exact 决定误差区间
    benchmark_is_exact: bool    # 收市价=精确；「約X.XX」的均价/每股净值=非精确
    stated_pct: Decimal         # 公告写的百分比，取绝对值
    stated_direction: str       # PREMIUM / DISCOUNT
    page: int
    source_quote: str


def _benchmark_interval(cmp_: PriceComparison) -> tuple[Decimal, Decimal]:
    """基准价的真值区间。精确值就是它自己；「約」值按印出的位数展开。"""
    if cmp_.benchmark_is_exact:
        return cmp_.benchmark, cmp_.benchmark
    half = Decimal(1).scaleb(-cmp_.benchmark_decimals) / 2
    return cmp_.benchmark - half, cmp_.benchmark + half


def signed_pct(offer_price: Decimal, benchmark: Decimal) -> Decimal:
    """要约价相对基准价的变动百分比。正=溢价，负=折让。"""
    return (offer_price / benchmark - 1) * 100


def v4_percentage_recompute(offer_price: Decimal,
                            cmp_: PriceComparison) -> Finding:
    """V4：公告写的百分比，能不能用公告自己的数字复算出来。"""
    low_base, high_base = _benchmark_interval(cmp_)
    # 基准价越大，百分比越小，所以上下界要交叉
    pct_low = signed_pct(offer_price, high_base)
    pct_high = signed_pct(offer_price, low_base)

    stated = cmp_.stated_pct if cmp_.stated_direction == PREMIUM else -cmp_.stated_pct
    passed = (pct_low - _PCT_ROUNDING) <= stated <= (pct_high + _PCT_ROUNDING)

    if cmp_.benchmark_is_exact:
        detail = (f"基准 {cmp_.benchmark}（精确）→ 复算 {pct_low:+.4f}%；"
                  f"公告 {stated:+.2f}%")
    else:
        detail = (f"基准印作「約{cmp_.benchmark}」→ 真值∈[{low_base}, {high_base}]"
                  f" → 百分比∈[{pct_low:+.4f}%, {pct_high:+.4f}%]；公告 {stated:+.2f}%")
    return Finding("V4", passed, cmp_.label, detail)


def v5_direction(offer_price: Decimal, cmp_: PriceComparison) -> Finding:
    """V5：要约价高于基准却写「折让」（或反之）——附录 D-2 型错误。

    数值对、方向错，这种错误光看百分比看不出来，必须比大小。
    """
    expected = PREMIUM if offer_price > cmp_.benchmark else DISCOUNT
    passed = expected == cmp_.stated_direction
    detail = (f"要约价 {offer_price} vs 基准 {cmp_.benchmark} → 应为"
              f"{'溢价' if expected == PREMIUM else '折让'}；"
              f"公告写{'溢价' if cmp_.stated_direction == PREMIUM else '折让'}")
    return Finding("V5", passed, cmp_.label, detail)


def v6_benchmark_in_range(cmp_: PriceComparison, low_6m: Decimal,
                          high_6m: Decimal, *, exempt: bool = False) -> Finding:
    """V6：市价基准落不落在公告自报的六个月高低价区间内。

    附录 D-1 就是靠这条抓出来的：均价 63.7 远超六个月最高价 0.70，
    说明单位印错了（应为 63.7 港仙）。

    exempt=True 用于每股净资产这类非市价基准 —— 它本来就不该受市价区间约束。
    另注意：窗口长于六个月的均价（如 180 个交易日 ≈ 8.5 个月）
    理论上可以落在区间外，判 FAIL 前需人工看一眼。
    """
    if exempt:
        return Finding("V6", True, cmp_.label, "非市价基准，不适用六个月区间")
    passed = low_6m <= cmp_.benchmark <= high_6m
    return Finding("V6", passed, cmp_.label,
                   f"基准 {cmp_.benchmark} vs 六个月区间 [{low_6m}, {high_6m}]")


def v3_closure(subject: str, parts: list[tuple[str, Decimal]], total: Decimal,
               tolerance: Decimal = Decimal("0")) -> Finding:
    """V3：分项加总应当闭合到公告自报的总数。

    容差默认 0 —— 股数和代价金额是精确值，对不上就是有问题。
    只有在已知存在取整（如要约价上浮）时才放容差，且必须写明理由。
    """
    summed = sum((v for _, v in parts), Decimal(0))
    diff = summed - total
    passed = abs(diff) <= tolerance
    expr = " + ".join(f"{n}({v:,})" for n, v in parts)
    return Finding("V3", passed, subject,
                   f"{expr} = {summed:,} vs 公告 {total:,}；差额 {diff:,}")


def v8_magnitude(subject: str, per_share_values: list[tuple[str, Decimal]],
                 max_ratio: Decimal = Decimal("20")) -> Finding:
    """V8：每股价格数量级异常。

    同一份公告里的每股口径数字（要约价、各基准价、高低价）本应在同一量级。
    附录 D-1 里 63.7 与 0.167 相差 381 倍，就是单位串了（港元/港仙）。
    """
    values = [v for _, v in per_share_values if v > 0]
    if len(values) < 2:
        return Finding("V8", True, subject, "可比数字不足两个，跳过")
    ratio = max(values) / min(values)
    passed = ratio <= max_ratio
    hi = max(per_share_values, key=lambda kv: kv[1])
    lo = min((kv for kv in per_share_values if kv[1] > 0), key=lambda kv: kv[1])
    return Finding("V8", passed, subject,
                   f"最大 {hi[0]}={hi[1]} / 最小 {lo[0]}={lo[1]} = {ratio:.1f}x"
                   f"（阈值 {max_ratio}x）")


def v15_discount_floor(subject: str, signed_pct: Decimal) -> Finding:
    """V15：折让不可能超过 100%。

    价格最低只能到 0，也就是 -100%；再往下要求要约价是负数，
    即要约人一边拿走股份一边收钱 —— 逻辑不成立。

    这条最早只用来查**答案表**（它在那儿抓到 01875 東曜的 -114.67%
    是符号笔误）。但程序自己也会把溢价写成折让，同一个算术约束
    对两边都成立，所以搬进校验器，对产出也跑一遍。
    """
    passed = signed_pct > Decimal("-100")
    return Finding("V15", passed, subject,
                   f"主值溢价率 {signed_pct}% —— 折让超过 100% 意味着要约价为负")


def run_price_comparisons(offer_price: Decimal, comparisons: list[PriceComparison],
                          low_6m: Decimal, high_6m: Decimal,
                          nonmarket_labels: frozenset[str] = frozenset()) -> list[Finding]:
    """对「价值比较」整节跑 V4 / V5 / V6。"""
    findings: list[Finding] = []
    for cmp_ in comparisons:
        findings.append(v4_percentage_recompute(offer_price, cmp_))
        findings.append(v5_direction(offer_price, cmp_))
        findings.append(v6_benchmark_in_range(
            cmp_, low_6m, high_6m, exempt=cmp_.label in nonmarket_labels))
    return findings

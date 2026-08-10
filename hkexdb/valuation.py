"""估值组：由 Python 从已摘录的数字算出来的派生指标。

**铁律一的落点。** 抽取层一个数都不算，只把公告印的数字连同出处摘下来；
所有乘除在这里做，每个结果都能说出「等于哪两个带出处的数字算出来的」。

这一层存在的理由，是「全部已發行股本估值不是垃圾，只是不能当 deal size」：

    deal size  = 要约项下应付的**最高现金代价**（付给接纳要约的公众股东）
    equity value = 已发行股数 × 要约价（这单把整家公司作价多少）

两个都要，但绝不能混。判别口诀：**这笔钱付给谁？**付给特定卖方的
不是要约规模，付给接纳要约的公众股东的才是。

壳股看 P/B 不看 P/E，所以每股 NAV 单独存、P/B 单独算 —— NAV 不做
溢价率主值，但它是估值组的核心。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _dec(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None


@dataclass
class Derived:
    """派生指标。每一项都附「怎么算出来的」，好让人对着公告核。"""

    implied_equity_value: str = ""     # 已发行股数 × 要约价
    implied_equity_basis: str = ""
    pb_ratio: str = ""                 # 要约价 ÷ 每股NAV
    pb_basis: str = ""
    runup_pct: str = ""                # 未受干扰日 → 最后交易日 的涨幅
    runup_basis: str = ""


def derive(offer_price: str, total_shares: str, nav_per_share: str,
           undisturbed_spot: str, last_trading_spot: str) -> Derived:
    """把摘录到的数字算成估值组指标。缺料就留空，绝不用别的数顶上。"""
    out = Derived()
    price = _dec(offer_price)

    shares = _dec(total_shares)
    if price is not None and shares is not None and shares > 0:
        out.implied_equity_value = f"{price * shares:.2f}"
        out.implied_equity_basis = f"{total_shares} 股 × {offer_price} 港元"

    nav = _dec(nav_per_share)
    if price is not None and nav is not None and nav > 0:
        out.pb_ratio = f"{price / nav:.3f}"
        out.pb_basis = f"{offer_price} ÷ 每股净资产 {nav_per_share}"

    # 泄露证据：不受干扰日到最后交易日之间股价跑了多少。
    # 跑得越多，说明消息越早漏出去，「最后交易日收市价」这个基准就越
    # 不能用 —— 它已经把要约的预期计进去了。这正是该用哪个基准的判据。
    a, b = _dec(undisturbed_spot), _dec(last_trading_spot)
    if a is not None and b is not None and a > 0:
        out.runup_pct = f"{(b / a - 1) * 100:.2f}"
        out.runup_basis = (f"最后交易日 {last_trading_spot} ÷ "
                           f"未受干扰日 {undisturbed_spot} − 1")
    return out


# ---------------------------------------------------------------- 交易性质

# 「要约有两个物种」：真收购（产业买家、正溢价）vs 买壳与技术性要约
# （壳公司、深折让、设计成零接纳）。混在一起算中位数＝废数据。
#
# ⚠️ 这是**分类**，按铁律二属于「错了就是静默污染」的那一层，所以：
#   · 这里只给**建议值**，永远带「待确认」，绝不当成已定论；
#   · 每条建议都必须说出依据是哪几个事实；
#   · 阈值放在 config.yaml，改了重跑，不手改结果。
DEEP_DISCOUNT_PCT = Decimal("-30")


@dataclass
class NatureGuess:
    label: str = ""
    reasons: list | None = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


def guess_nature(*, premium_pct: str, listing_intent: str,
                 debt_conversion: bool, offer_type: str, runup_pct: str = "",
                 deep_discount_pct: Decimal = DEEP_DISCOUNT_PCT) -> NatureGuess:
    """猜这单属于哪个物种。**只是建议**，等人确认。

    投行做可比时第一件事就是把买壳/技术性要约剔出去 —— 它们的溢价率
    根本不是在给控制权定价。没有这个标记，中位数没有意义。
    """
    reasons: list[str] = []
    premium = _dec(premium_pct)

    if debt_conversion:
        reasons.append("正文出现债转股／换股价字样（被动触发规则26.1的典型）")
    if listing_intent == "拟撤销上市":
        reasons.append("要约人表明拟撤销上市地位")
    if premium is not None:
        if premium <= deep_discount_pct:
            reasons.append(f"主值折让 {premium}%，深折让是买壳与技术性要约的特征")
        elif premium > 0:
            reasons.append(f"主值溢价 +{premium}%，付了控制权溢价")

    # 泄露证据：消息漏得越早，「最后交易日收市价」这个基准越没用 ——
    # 它已经把要约的预期计进去了。这条不参与分类，但要摆在依据里。
    runup = _dec(runup_pct)
    if runup is not None and abs(runup) >= 10:
        reasons.append(f"未受干扰日到最后交易日股价跑了 {runup}%，"
                       f"消息提前反映，慎用最后交易日口径")

    if listing_intent == "拟撤销上市":
        label = "私有化"
    elif debt_conversion and premium is not None and premium <= 0:
        label = "技术性要约"
    elif premium is not None and premium <= deep_discount_pct:
        label = "疑似买壳／技术性"
    elif premium is not None and premium > 0:
        label = "疑似产业收购"
    else:
        # 信号不指向任何一类。空着会让人以为「没这回事」，
        # 而实际是「判不出来」—— 这两件事不一样，必须说清楚。
        return NatureGuess("未分类（需人工判断）", reasons)

    return NatureGuess(f"{label}（待确认）", reasons)

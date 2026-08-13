"""主值选取：一份公告里有多个候选值，选哪一个进数据库。

⚠️ **这里的规则是从三单人工答案反推出来的，不是附录 A。**
附录 A 尚未提供。三单样本一致并不等于规则正确——它可能在第四单就失效。
附录 A 到手后必须逐条核对，以附录 A 为准。

反推依据（`tests/test_selectors.py` 里有对应测试）：

| 单 | 你的溢价率答案 | 落在哪一项 |
| --- | --- | --- |
| 1417 MGO | −55.57% | 最後交易日前 30 日均价（该单无未受干扰日）|
| 3336 VGO | −15.45% | **未受干扰日**前 30 日均价（最後交易日前 30 日是 −17.50%，未选）|
| 00195 PO | +4.49% | 最後交易日前 30 日均价（该单无未受干扰日）|

→ 溢价率规则：**30 日均价；存在未受干扰日锚点时优先取未受干扰日。**

| 单 | 你的交易规模答案 | 是什么 |
| --- | --- | --- |
| 1417 MGO | 54,400,000 | 要约项下最高现金代价（非 SPA 对价 155,643,703）|
| 3336 VGO | 1,905,849,908.60 | 要约悉数接纳时最高现金（非 SPA 对价 734,168,670.40）|
| 00195 PO | 92,000,000 | 部分要约悉数接纳时现金代价总额 |

→ 交易规模规则：**要约项下最高现金代价**，不是控股权转让对价，
  也不是 100% 股本隐含估值。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# 锚点优先级：靠前的优先。未受干扰日排在最后交易日之前 —— 这是三单反推的结果。
DEFAULT_ANCHOR_PRIORITY = ("undisturbed", "last_trading_day", "pre_rule37")

DEFAULT_WINDOW = "30d"

# 没有 30 日均价时按这个顺序退。**退到哪一档会写进「主值口径」那一列**，
# 所以这不是静默降级 —— 数字和口径永远同列显示。
#
# 顺序＝**离 30 个交易日最近的先上**（月按 21 个交易日折算），
# 你说的「取 30 天以内或者 30 天之后的数」就是这个意思：
#   30d(0) 1m(9) 10d(20) 5d(25) 收市价(29) 60d(30) 3m(33) 90d(60)
#   120d(90) 6m(96) 180d(150) 12m(222)
# 依据（两单都是你答案表里核过的）：
#   01833 平安好醫生  答案 -4.23%  ← 前10日均价（该单最长只到 10 日）
#   01980 天鴿互動    答案 +2.10%  ← 前5日均价（该单最长只到 5 日）
#   09638 法拉帝      答案 +27.20% ← 前1个月均价（整套梯子按月，没有交易日口径）
# 原来这几单一律留空，等于把「公告没给 30 日」当成「公告没给溢价率」。
WINDOW_FALLBACK = ("30d", "1m", "10d", "5d", "spot", "60d", "3m",
                   "90d", "120d", "6m", "180d", "12m")


@dataclass(frozen=True)
class PremiumPick:
    """选中的溢价率，连同为什么选它。铁律三：结论要能追溯。"""

    signed_pct: Decimal          # 正=溢价，负=折让
    anchor: str
    window: str
    label: str
    page: int
    source_quote: str
    considered: int              # 一共有多少项候选
    rejected: list[str]          # 同窗口下被更高优先级锚点挤掉的项
    benchmark: str = ""          # 这一项的基准价原文，方向复核要用
    stated_pct: str = ""         # 公告印的百分比原文（不带符号）
    stated_direction: str = ""   # 公告写的是溢价还是折让
    fell_back: bool = False      # 公告没有 30 日均价，退到了更短的窗口
    other_anchor: str = ""       # 同窗口下另一个锚点的口径（例：最后交易日前30日均价）
    other_pct: str = ""          # 那一项的带符号百分比


def _pick_one(candidates: list[dict], anchor_priority: tuple[str, ...],
              total: int, fell_back: bool) -> PremiumPick | None:
    def rank(item: dict) -> tuple:
        # 交易所排在锚点**前面**：双重上市的公司两套梯子数字完全不同
        # （09638 法拉帝对米兰 25.9%、对港交所 27.2%），这是港股可比库，
        # 口径取港交所那一套。认不出交易所的按港股处理（绝大多数单）。
        venue = 0 if item.get("venue", "") != "foreign" else 1
        anchor = item.get("anchor", "")
        return (venue, anchor_priority.index(anchor)
                if anchor in anchor_priority else len(anchor_priority))

    best = min(candidates, key=rank)
    if rank(best)[1] >= len(anchor_priority):
        return None      # 该窗口下没有任何已知锚点，宁可不给答案

    pct = Decimal(str(best["stated_pct"]))
    signed = pct if best["stated_direction"] == "premium" else -pct

    # 同一个窗口下另一个锚点的那一项。08439 和 3336 两单说明这个选择
    # 有争议（一单你要最后交易日，一单你要未受干扰日），所以不管选了
    # 哪个，都把另一个摆到备注里 —— 想改哪一单，看一眼就能改。
    others = [c for c in candidates if c is not best
              and rank(c)[1] < len(anchor_priority)]
    other = min(others, key=rank) if others else None
    other_pct = ""
    if other is not None:
        value = Decimal(str(other["stated_pct"]))
        other_pct = str(value if other["stated_direction"] == "premium" else -value)

    return PremiumPick(
        signed_pct=signed,
        anchor=best["anchor"],
        window=best["window"],
        label=best["label"],
        page=best["page"],
        source_quote=best["quote"],
        considered=total,
        rejected=[c["label"] for c in candidates if c is not best],
        benchmark=str(best.get("benchmark", "")),
        stated_pct=str(best["stated_pct"]),
        stated_direction=best["stated_direction"],
        fell_back=fell_back,
        other_anchor=other["label"] if other is not None else "",
        other_pct=other_pct,
    )


def select_primary_premium(comparisons: list[dict], *,
                           window: str = DEFAULT_WINDOW,
                           anchor_priority: tuple[str, ...] = DEFAULT_ANCHOR_PRIORITY,
                           fallback: tuple[str, ...] = WINDOW_FALLBACK
                           ) -> PremiumPick | None:
    """从「价值比较」各项里选出主值溢价率。

    comparisons 的每一项须带结构化的 anchor / window 字段 ——
    **不要用中文标签做字符串匹配**：三单里同一个概念就有
    「前30个交易日平均收市价」「最後交易日前30日均价」等多种写法。

    先要 30 日均价；公告压根没印 30 日的，按 `fallback` 一档档退到
    10 日 / 5 日 / 收市价。退到哪一档会原样写进「主值口径」，和数字同列，
    所以它不是静默降级 —— 看表的人一眼知道这个 -4.23% 是 10 日口径。

    该窗口下一个已知锚点都没有时仍然返回 None：
    宁可留空，也不挑一个说不出出处的近似值。
    """
    windows = [window] + [w for w in fallback if w != window]
    for index, name in enumerate(windows):
        candidates = [c for c in comparisons if c.get("window") == name]
        if not candidates:
            continue
        pick = _pick_one(candidates, anchor_priority, len(comparisons),
                         fell_back=index > 0)
        if pick is not None:
            return pick
    return None


# ------------------------------------------------------- 主值方向的算术复核

@dataclass(frozen=True)
class DirectionCheck:
    """主值那一项的「溢价/折让」两个字，和它自己的两个数字对不对得上。"""

    agrees: bool                 # 措辞与算术一致
    arithmetic: str              # 按两个数字算出来的方向
    stated: str                  # 公告写的方向
    pct_agrees: bool             # 公告印的百分比能不能用这两个数复算出来
    detail: str                  # 写给人看的一句话，带上两个数


_PCT_TOLERANCE = Decimal("0.6")   # 百分点。印刷四舍五入 + 基准价取「約」值


def check_direction(pick: PremiumPick | None, offer_price: str) -> DirectionCheck | None:
    """用公告自己的两个数字，复核公告自己的那两个字（铁律一）。

    准确率报告里有三单的溢价率**符号相反**。符号错和数值错不是同一
    类错：数值差一点还能用，符号反了会把折让当成溢价放进可比表，
    而做 precedent 时那一行会直接得出相反的结论。

    这里只做比大小和一次除法，不做任何判断：

        要约价 > 基准价 → 算术上就是溢价，公告写「折让」两个字就对不上。

    对不上时还要再问一句：公告印的**百分比数值**能不能用这两个数
    复算出来？
        能   → 数字自洽，错的只是那两个字（公告的印刷错误，你手册里
               「公告会错」那一节说的就是这个）。这时算术说了算。
        不能 → 多半是我把基准价配错了行（PDF 表格错位那类），
               两边都不可信，只能标出来给人看。

    返回 None 表示无从复核（缺要约价或缺基准价）—— 不复核就不表态。
    """
    if pick is None:
        return None
    try:
        offer = Decimal(str(offer_price).replace(",", "").strip())
        benchmark = Decimal(str(pick.benchmark).replace(",", "").strip())
        stated_pct = Decimal(str(pick.stated_pct).replace(",", "").strip())
    except (ArithmeticError, ValueError, TypeError):
        return None
    if benchmark <= 0 or offer <= 0:
        return None

    arithmetic = "premium" if offer > benchmark else "discount"
    computed = abs(offer - benchmark) / benchmark * 100
    pct_agrees = abs(computed - stated_pct) <= _PCT_TOLERANCE
    word = {"premium": "溢价", "discount": "折让"}
    detail = (f"要约价 {offer} 对基准 {benchmark}（{pick.label}）"
              f"→ 算术上是{word[arithmetic]} {computed:.2f}%；"
              f"公告写{word.get(pick.stated_direction, pick.stated_direction)}"
              f" {stated_pct}%")
    return DirectionCheck(agrees=arithmetic == pick.stated_direction,
                          arithmetic=arithmetic, stated=pick.stated_direction,
                          pct_agrees=pct_agrees, detail=detail)


# ⚠️ 交易规模的选取**不在这一层**。它在 extractor.deal_size_candidates ——
# 那里能看见原文上下文（「这笔钱付给谁」得看句子，看不了候选清单）。
# 这里曾经有第二套实现 select_primary_deal_size，流水线从来没调用过，
# 只有它自己的测试在用。两套规则各说各话是审计级数据库最不该有的东西，
# 所以删掉了。交易规模的规则和回归测试都在抽取层。

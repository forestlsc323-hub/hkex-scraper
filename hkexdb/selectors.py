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


def select_primary_premium(comparisons: list[dict], *,
                           window: str = DEFAULT_WINDOW,
                           anchor_priority: tuple[str, ...] = DEFAULT_ANCHOR_PRIORITY
                           ) -> PremiumPick | None:
    """从「价值比较」各项里选出主值溢价率。

    comparisons 的每一项须带结构化的 anchor / window 字段 ——
    **不要用中文标签做字符串匹配**：三单里同一个概念就有
    「前30个交易日平均收市价」「最後交易日前30日均价」等多种写法。

    选不出来时返回 None，绝不退而求其次挑一个近似的。
    静默降级正是铁律二说的那种污染。
    """
    candidates = [c for c in comparisons if c.get("window") == window]
    if not candidates:
        return None

    def rank(item: dict) -> int:
        anchor = item.get("anchor", "")
        return anchor_priority.index(anchor) if anchor in anchor_priority else len(anchor_priority)

    best = min(candidates, key=rank)
    if rank(best) >= len(anchor_priority):
        return None      # 该窗口下没有任何已知锚点，宁可不给答案

    pct = Decimal(str(best["stated_pct"]))
    signed = pct if best["stated_direction"] == "premium" else -pct

    return PremiumPick(
        signed_pct=signed,
        anchor=best["anchor"],
        window=best["window"],
        label=best["label"],
        page=best["page"],
        source_quote=best["quote"],
        considered=len(comparisons),
        rejected=[c["label"] for c in candidates if c is not best],
    )


@dataclass(frozen=True)
class DealSizePick:
    value: Decimal
    key: str
    role: str
    page: int | None
    source_quote: str | None
    rejected: list[str]


def select_primary_deal_size(candidates: list[dict], *,
                             key: str = "offer_max_cash") -> DealSizePick | None:
    """从交易规模的多个候选值里选主值。

    1417 那单有 5 个候选，其中 2 个是分项干扰值。
    按反推规则取 `offer_max_cash`（要约项下最高现金代价）。
    """
    match = next((c for c in candidates if c.get("key") == key), None)
    if match is None:
        return None
    return DealSizePick(
        value=Decimal(str(match["value"])),
        key=match["key"],
        role=match.get("role", ""),
        page=match.get("page"),
        source_quote=match.get("quote"),
        rejected=[c["key"] for c in candidates if c is not match],
    )

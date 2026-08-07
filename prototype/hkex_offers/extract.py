"""Pull premium / discount percentages out of offer announcement text."""

from __future__ import annotations

import re
from dataclasses import dataclass

PREMIUM = "premium"
DISCOUNT = "discount"

_NUM = r"[0-9]{1,3}(?:\.[0-9]+)?"
_PCT = r"\s*[%％]"

_ZH_KIND = {"溢價": PREMIUM, "溢价": PREMIUM, "折讓": DISCOUNT, "折让": DISCOUNT,
            "折價": DISCOUNT, "折价": DISCOUNT}

_PATTERNS = [
    # 溢價約 20.5%  /  折讓 5%
    re.compile(rf"(溢價|溢价|折讓|折让|折價|折价)(?:率)?\s*(?:約|约|大約|大约)?\s*({_NUM}){_PCT}"),
    # 20.5% 的溢價
    re.compile(rf"({_NUM}){_PCT}\s*(?:的)?\s*(溢價|溢价|折讓|折让|折價|折价)"),
    # premium of approximately 20.5%
    re.compile(
        rf"\b(premium|discount)\b\s+(?:of\s+)?(?:approximately|about|around|circa|ca\.?)?\s*({_NUM}){_PCT}",
        re.IGNORECASE,
    ),
    # 20.5% premium over
    re.compile(rf"({_NUM}){_PCT}\s+(premium|discount)\b", re.IGNORECASE),
]

_BENCHMARKS = [
    ("last_trading_day_close", re.compile(
        r"最後交易日|最后交易日|最後全日交易日|last\s+trading\s+day", re.IGNORECASE)),
    ("pre_suspension_close", re.compile(
        r"停牌前|暫停買賣前|暂停买卖前|prior\s+to\s+(?:the\s+)?suspension", re.IGNORECASE)),
    ("avg_5_days", re.compile(r"五個?交易日|5\s*個?交易日|(?:last|previous)\s+(?:five|5)\s+", re.IGNORECASE)),
    ("avg_10_days", re.compile(r"十個?交易日|10\s*個?交易日|(?:last|previous)\s+(?:ten|10)\s+", re.IGNORECASE)),
    ("avg_30_days", re.compile(r"三十個?交易日|30\s*個?交易日|(?:last|previous)\s+(?:thirty|30)\s+", re.IGNORECASE)),
    ("nav", re.compile(r"資產淨值|资产净值|每股淨值|net\s+asset\s+value|\bNAV\b", re.IGNORECASE)),
]

# Preferred benchmark order when an announcement quotes several comparisons.
_BENCHMARK_PRIORITY = [
    "last_trading_day_close",
    "pre_suspension_close",
    "avg_5_days",
    "avg_10_days",
    "avg_30_days",
    "",
    "nav",
]

_CONTEXT = 140

# Announcements list several comparisons back to back, so the benchmark must be
# read from the clause holding the percentage, not from the surrounding context.
_CLAUSE_BREAK = re.compile(r"[。；！？\n]|(?<=[a-z0-9\)])\.(?=\s+[A-Z(])")


@dataclass(frozen=True)
class Hit:
    kind: str  # PREMIUM or DISCOUNT
    pct: float
    benchmark: str
    context: str
    pos: int


def _kind_of(token: str) -> str:
    token = token.strip()
    if token in _ZH_KIND:
        return _ZH_KIND[token]
    return PREMIUM if token.lower() == "premium" else DISCOUNT


def _benchmark_of(context: str) -> str:
    for name, pattern in _BENCHMARKS:
        if pattern.search(context):
            return name
    return ""


def _clause(flat: str, start: int, end: int) -> str:
    left = 0
    right = len(flat)
    for m in _CLAUSE_BREAK.finditer(flat):
        if m.end() <= start:
            left = m.end()
        elif m.start() >= end:
            right = m.start()
            break
    return flat[left:right].strip()


def _normalize(text: str) -> str:
    return re.sub(r"[ \t 　]+", " ", text.replace("\n", " "))


def find_hits(text: str) -> list[Hit]:
    """All premium/discount mentions in the text, de-duplicated by position."""
    flat = _normalize(text)
    hits: dict[int, Hit] = {}

    for pattern in _PATTERNS:
        for m in pattern.finditer(flat):
            a, b = m.group(1), m.group(2)
            token, number = (a, b) if not a[0].isdigit() else (b, a)
            try:
                pct = float(number)
            except ValueError:
                continue
            if pct > 1000:
                continue
            hits[m.start()] = Hit(
                kind=_kind_of(token),
                pct=pct,
                benchmark=_benchmark_of(_clause(flat, m.start(), m.end())),
                context=flat[max(0, m.start() - _CONTEXT): m.end() + _CONTEXT].strip(),
                pos=m.start(),
            )

    return [hits[k] for k in sorted(hits)]


def pick_primary(hits: list[Hit]) -> Hit | None:
    """The hit most likely to be the headline premium/discount vs. the last close."""
    if not hits:
        return None
    def rank(hit: Hit) -> tuple[int, int]:
        try:
            benchmark_rank = _BENCHMARK_PRIORITY.index(hit.benchmark)
        except ValueError:
            benchmark_rank = len(_BENCHMARK_PRIORITY)
        return (benchmark_rank, hit.pos)
    return min(hits, key=rank)

"""Search keywords and offer-type classification rules."""

from __future__ import annotations

import re

# Broad title keywords; recall first, precision comes from classify_offer_type().
DEFAULT_KEYWORDS = {
    "ZH": ["要約", "要约"],
    "EN": ["OFFER"],
}

# Titles matching these are not takeover offers (bond tenders, buy-backs, ...).
EXCLUDE_TITLE = re.compile(
    r"債券|债券|票據|票据|購回|购回|回購|回购|認購|认购|供股|"
    r"\bbond\b|\bnote(s)?\b|buy-?back|repurchase|rights\s+issue|subscription",
    re.IGNORECASE,
)

MGO = "MGO"
VGO = "VGO"
PO = "PO"

_PARTIAL = re.compile(r"部分要約|部分要约|partial\s+offer", re.IGNORECASE)
_MANDATORY = re.compile(r"強制性|强制性|mandatory", re.IGNORECASE)
_VOLUNTARY = re.compile(r"自願|自愿|voluntary", re.IGNORECASE)
_OFFER = re.compile(r"要約|要约|\boffer\b", re.IGNORECASE)


def classify_offer_type(text: str) -> str | None:
    """Return MGO / VGO / PO, or None when the text is not a general-offer notice."""
    if not text or not _OFFER.search(text):
        return None
    if _PARTIAL.search(text):
        return PO
    if _MANDATORY.search(text):
        return MGO
    if _VOLUNTARY.search(text):
        return VGO
    return None

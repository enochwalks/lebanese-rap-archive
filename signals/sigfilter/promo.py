"""Detect recruitment and deposit-scam posts, and hold them against the channel.

A channel whose feed is mostly "invest $1000 get $11,000", "slot is limited" and
"contact admin for account management" is not a signal channel with ads attached;
it is a funnel with signals attached. The signals it does post exist to build the
credibility that the funnel spends, so its trust score should reflect the whole
feed, not just the posts that happen to parse as trades.
"""

import re

SCAM_PATTERNS = [
    r"invest(?:ment)?\s*(?:plan|plans)?\s*[:\-]?\s*\$?\s*[0-9,]+",
    r"\binvestment\s*plans?\b",
    r"\$\s*[0-9,]{3,}\s*(?:and|or)\s*above\b",
    r"\bmake\s*it\s*big\b",
    r"\b[0-9]+\s*days?\s*plans?\b",
    r"\$\s*[0-9,]+\s*(?:->|→|to|get|gets|turns? into|becomes?)\s*\$?\s*[0-9,]+",
    r"\bget\s*\$\s*[0-9,]{3,}",
    r"\bslots?\s*(?:is|are)?\s*limited\b",
    r"\bcontact\s*(?:the\s*)?admin\b",
    r"\bdm\s*(?:me|admin|us)\b",
    r"\baccount\s*manage(?:ment|r)\b",
    r"\bmanage\s*your\s*account\b",
    r"\bbook\s*your\s*slot\b",
    r"\bcopy\s*trad(?:e|ing)\s*(?:service|plan)\b",
    r"\bprofit\s*shar(?:e|ing)\b",
    r"\bcommission\s*is\s*[0-9]{1,3}\s*%",
    r"\bwithdraw(?:al)?\s*method",
    r"\bperfect\s*money\b",
    r"\bsend\s*(?:me|us)\s*(?:btc|usdt|money|payment)\b",
    r"\bvip\s*(?:sub|subscription|membership|access)\b.*\b(?:pay|price|\$)",
    r"\bguaranteed\s*(?:profit|return|income)\b",
    r"\bdouble\s*your\s*(?:money|capital|investment)\b",
    r"استثمر|ارباح مضمونة|ربح مضمون|تواصل مع الادمن",
]

_COMPILED = [re.compile(p, re.I | re.U) for p in SCAM_PATTERNS]


def scam_hits(text):
    """Which recruitment patterns this message matches."""
    if not text:
        return []
    return [p.pattern for p in _COMPILED if p.search(text)]


def is_promo(text):
    return bool(scam_hits(text))


def penalty(promo_count, total_count, floor=0.35):
    """Trust multiplier from a channel's promo ratio.

    Needs a real sample before biting: three promo posts on day one says nothing,
    but a third of the feed being deposit pitches says plenty.
    """
    if total_count < 20:
        return 1.0
    ratio = promo_count / float(total_count)
    if ratio < 0.10:
        return 1.0
    return max(floor, 1.0 - min(0.65, ratio))

"""Cross-channel agreement — and the trap inside it.

Most signal channels copy each other. Three channels posting the same text
15 seconds apart is one source wearing three hats, and counting it as
"3 channels agree" is the single easiest way to build a confident filter that
is confidently wrong. So near-identical text collapses to one vote.
"""

import difflib
import re
import time

_NORMALISE_RE = re.compile(r"[^a-z0-9 ]+")


def normalise(text):
    """Strip emoji, branding and punctuation so copy-paste shows through."""
    lowered = (text or "").lower()
    lowered = re.sub(r"(?:https?://\S+|@[\w_]+|t\.me/\S+)", " ", lowered)
    lowered = _NORMALISE_RE.sub(" ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def similarity(a, b):
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def evaluate(conn, sig, channel_id, now=None, window_minutes=45, near_duplicate_ratio=0.82):
    """Returns dict: agreeing channel count, conflicts, duplicate flag."""
    from . import db

    now = now or int(time.time())
    since = now - window_minutes * 60
    rows = db.recent_for_symbol(conn, sig.symbol, sig.side, since)

    agree_channels, conflict_channels, duplicate_of = set(), set(), None
    texts_by_channel = {}

    for row in rows:
        if row["channel_id"] == channel_id and row["msg_id"]:
            continue
        if row["side"] == sig.side:
            agree_channels.add(row["channel_id"])
            texts_by_channel.setdefault(row["channel_id"], row["text"])
        elif row["side"]:
            conflict_channels.add(row["channel_id"])

    # Collapse relayed copies: if this message is a near-copy of an earlier one,
    # it is not independent confirmation.
    for other_channel, other_text in texts_by_channel.items():
        if similarity(sig.raw, other_text) >= near_duplicate_ratio:
            duplicate_of = other_channel
            agree_channels.discard(other_channel)

    return {
        "agreeing_channels": len(agree_channels),
        "conflicting_channels": len(conflict_channels),
        "duplicate_of": duplicate_of,
        "window_minutes": window_minutes,
    }

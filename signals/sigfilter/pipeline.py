"""ingest -> parse -> dedupe -> consensus -> score -> gate -> deliver."""

import hashlib
import time

from . import consensus, db, parse, promo, score
from .config import source_map


def fingerprint(sig):
    """Same idea from the same source shouldn't count twice if it's reposted."""
    parts = [sig.symbol or "", sig.side or "",
             f"{sig.entry:.6g}" if sig.entry else "",
             f"{sig.sl:.6g}" if sig.sl else ""]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def process(conn, cfg, *, channel_id, msg_id, text, ts, now=None):
    """Evaluate one message. Returns a result dict; caller handles delivery."""
    now = now or int(time.time())
    sources = source_map(cfg)
    channel = sources.get(channel_id, {"name": str(channel_id), "weight": 1.0})

    if db.already_seen(conn, channel_id, msg_id):
        return {"status": "duplicate_message"}

    # Count the whole feed, not just the tradeable part: the promo ratio is what
    # tells you whether this is a signal channel or a funnel.
    db.bump_counter(conn, f"msgs:{channel_id}")
    if promo.is_promo(text):
        db.bump_counter(conn, f"promo:{channel_id}")

    sig = parse.parse(text)
    if not sig:
        return {"status": "not_a_signal"}

    age = max(0, now - ts)
    info = consensus.evaluate(
        conn, sig, channel_id, now=now,
        window_minutes=cfg["consensus"]["window_minutes"],
        near_duplicate_ratio=cfg["consensus"]["near_duplicate_ratio"],
    )

    wins, losses = db.channel_record(conn, channel_id)
    promo_count = db.get_counter(conn, f"promo:{channel_id}")
    msg_count = db.get_counter(conn, f"msgs:{channel_id}")
    promo_penalty = promo.penalty(promo_count, msg_count)
    trust = score.channel_trust(
        wins, losses,
        prior_trades=cfg["scoring"]["trust_prior_trades"],
        weight=channel["weight"] * promo_penalty,
    )

    total, breakdown = score.score_signal(
        sig, trust=trust, consensus_info=info, age_seconds=age, cfg=cfg
    )
    forwarded = db.forwarded_today(conn, now)
    reasons = score.gate_reasons(sig, total, info, age, cfg, forwarded_today=forwarded)
    verdict = "ACCEPT" if not reasons else "REJECT"

    signal_id = db.record(
        conn, sig, channel_id, channel["name"], msg_id, ts,
        total, verdict, reasons or list(breakdown.keys()), fingerprint(sig),
    )

    return {
        "status": "scored",
        "signal_id": signal_id,
        "signal": sig,
        "score": total,
        "verdict": verdict,
        "reasons": reasons,
        "breakdown": breakdown,
        "consensus": info,
        "channel": channel["name"],
        "trust": round(trust, 3),
        "record": (wins, losses),
        "promo": (promo_count, msg_count, round(promo_penalty, 2)),
    }

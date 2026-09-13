"""Scoring and the accept/reject gate.

Every component returns 0..1 and is multiplied by its configured weight, so the
breakdown that ships with each forwarded signal is auditable: you can always see
which component carried it over the line.
"""

import math


def wilson_lower_bound(wins, total, z=1.96):
    """Lower bound of the win-rate confidence interval.

    A channel that is 3-for-3 must not outrank one that is 60-for-100; this is
    what stops a lucky new channel from hijacking the trust weight.
    """
    if total <= 0:
        return 0.0
    phat = wins / total
    denom = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


def channel_trust(wins, losses, prior_trades=10, weight=1.0):
    """0..1. Untested channels sit at ~0.5 and drift with evidence."""
    total = wins + losses
    if total == 0:
        return 0.5 * weight
    evidence = wilson_lower_bound(wins, total)
    # Blend toward the 0.5 prior until enough graded trades exist.
    confidence = min(1.0, total / float(prior_trades or 1))
    blended = 0.5 * (1 - confidence) + evidence * confidence
    return max(0.0, min(1.0, blended * weight))


def completeness(sig):
    have = [bool(sig.entries), bool(sig.tps), sig.sl is not None, bool(sig.symbol and sig.side)]
    base = sum(have) / len(have)
    if len(sig.tps) >= 2:
        base = min(1.0, base + 0.05)
    if sig.parse_notes:
        base -= 0.15 * len(sig.parse_notes)
    return max(0.0, min(1.0, base))


def risk_reward_score(rr, minimum=1.2):
    """Flat 0 below the minimum, saturating at 3R — beyond that the extra
    'reward' is usually a fantasy target the price never reaches."""
    if rr is None:
        return 0.0
    if rr < minimum:
        return 0.0
    return min(1.0, (rr - minimum) / (3.0 - minimum)) if rr < 3.0 else 1.0


def consensus_score(info):
    """Independent agreement helps; disagreement hurts more than agreement helps."""
    agree = info.get("agreeing_channels", 0)
    conflict = info.get("conflicting_channels", 0)
    if info.get("duplicate_of") is not None and agree == 0:
        return 0.35                      # relayed copy: no new information, but no penalty either
    base = {0: 0.5, 1: 0.75, 2: 0.9}.get(agree, 1.0)
    base -= 0.3 * conflict
    return max(0.0, min(1.0, base))


def discipline_score(sig, max_leverage=20):
    """Penalties for the tells that mark a channel as a casino, not a desk."""
    score = 1.0
    if sig.sl is None:
        score -= 0.5
    if sig.leverage and sig.leverage > max_leverage:
        score -= 0.4
    elif sig.leverage and sig.leverage > 10:
        score -= 0.15
    score -= 0.25 * len(sig.hype_hits)
    if len(sig.tps) > 4:
        score -= 0.1                      # target ladders long enough to always "hit TP1"
    raw = sig.raw or ""
    letters = [c for c in raw if c.isalpha()]
    if len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        score -= 0.1
    return max(0.0, min(1.0, score))


def freshness_score(age_seconds, max_age_minutes=20):
    """Linear decay. A 40-minute-old entry price is usually already gone."""
    if age_seconds is None:
        return 0.5
    max_age = max_age_minutes * 60
    if age_seconds <= 60:
        return 1.0
    if age_seconds >= max_age:
        return 0.0
    return 1.0 - (age_seconds - 60) / float(max_age - 60)


def score_signal(sig, *, trust, consensus_info, age_seconds, cfg):
    weights = cfg["scoring"]["weights"]
    gate = cfg["gate"]
    components = {
        "channel_trust": trust,
        "completeness": completeness(sig),
        "risk_reward": risk_reward_score(sig.risk_reward(), gate["min_risk_reward"]),
        "consensus": consensus_score(consensus_info),
        "discipline": discipline_score(sig, gate["max_leverage"]),
        "freshness": freshness_score(age_seconds, gate["max_age_minutes"]),
    }
    total_weight = sum(weights.get(k, 0) for k in components) or 1
    score = sum(components[k] * weights.get(k, 0) for k in components) / total_weight * 100
    breakdown = {
        k: {"value": round(components[k], 3), "weight": weights.get(k, 0),
            "points": round(components[k] * weights.get(k, 0) / total_weight * 100, 1)}
        for k in components
    }
    return round(score, 1), breakdown


def gate_reasons(sig, score, consensus_info, age_seconds, cfg, forwarded_today=0):
    """Hard rules that override the score. Returns list of rejection reasons."""
    gate = cfg["gate"]
    reasons = []
    if gate.get("require_stop_loss", True) and sig.sl is None:
        reasons.append("no stop-loss")
    rr = sig.risk_reward()
    if rr is None:
        reasons.append("cannot compute risk/reward (missing entry, target or stop)")
    elif rr < gate["min_risk_reward"]:
        reasons.append(f"risk/reward {rr:.2f} below minimum {gate['min_risk_reward']}")
    if sig.leverage and sig.leverage > gate["max_leverage"]:
        reasons.append(f"leverage {sig.leverage}x above maximum {gate['max_leverage']}x")
    if age_seconds is not None and age_seconds > gate["max_age_minutes"] * 60:
        reasons.append(f"stale by {int(age_seconds / 60)} minutes")
    if consensus_info.get("conflicting_channels", 0) > 0:
        reasons.append(f"{consensus_info['conflicting_channels']} channel(s) posting the opposite direction")
    if score < gate["min_score"]:
        reasons.append(f"score {score} below threshold {gate['min_score']}")
    if forwarded_today >= gate.get("daily_cap", 999):
        reasons.append(f"daily cap of {gate['daily_cap']} already reached")
    return reasons

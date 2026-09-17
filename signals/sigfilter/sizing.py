"""Position sizing — the part that actually keeps a small account alive.

The filter decides WHICH trades. This decides HOW BIG. On a small account the
lot size, not the signal, is what blows you up: gold's smallest lot already risks
several percent per trade, so this makes that cost explicit and refuses to hide it.
"""

import math

# Loss in account currency per 1.0 lot per 1.0 of price movement.
#   metals (XAUUSD): 1 lot = 100 oz, so $1 move = $100
#   forex majors:    1 lot = 100k units, so 1.0 of price = $100,000 (USD-quoted)
CONTRACT = {"metal": 100.0, "forex": 100000.0}
MIN_LOT = 0.01


def suggest(asset_class, entry, sl, balance, risk_percent):
    """Return a sizing dict, or None if we can't size this instrument safely.

    Keys: distance, risk_target ($), risk_at_min_lot ($), pct_at_min_lot,
    lot (rounded down to a 0.01 step), risk_at_lot ($), pct_at_lot, too_big.
    """
    mult = CONTRACT.get(asset_class)
    if not mult or entry is None or sl is None or not balance:
        return None
    distance = abs(entry - sl)
    if distance <= 0:
        return None

    risk_target = balance * (risk_percent / 100.0)
    per_min_lot = distance * MIN_LOT * mult          # $ risked by the smallest tradeable lot
    raw = risk_target / (distance * mult)
    lot = math.floor(raw / MIN_LOT) * MIN_LOT

    too_big = lot < MIN_LOT                           # even one min lot exceeds the risk target
    if too_big:
        lot = MIN_LOT
    risk_at_lot = distance * lot * mult
    return {
        "distance": round(distance, 2),
        "risk_target": round(risk_target, 2),
        "risk_at_min_lot": round(per_min_lot, 2),
        "pct_at_min_lot": round(100 * per_min_lot / balance, 1),
        "lot": round(lot, 2),
        "risk_at_lot": round(risk_at_lot, 2),
        "pct_at_lot": round(100 * risk_at_lot / balance, 1),
        "too_big": too_big,
    }

"""Grade past signals so channel trust is earned, not assumed.

Without this the filter is just a pretty regex: every channel stays at the 0.5
prior forever and 'trust' means nothing. Crypto is graded against Binance's free
public klines endpoint (no key, no account). Forex/metals need a data source
with an API key, so they are left ungraded rather than guessed at.
"""

import time

import requests

from . import db

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def _klines(symbol, start_ms, end_ms):
    try:
        resp = requests.get(
            BINANCE_KLINES,
            params={"symbol": symbol, "interval": "5m", "startTime": start_ms,
                    "endTime": end_ms, "limit": 1000},
            timeout=20,
        )
        if resp.status_code != 200:
            return []
        return resp.json()
    except (requests.RequestException, ValueError):
        return []


def grade_signal(row, horizon_hours=24, now=None):
    """WIN if TP1 trades before SL, LOSS if SL first, None if still open.

    Within a 5-minute candle that touches both levels we cannot tell which came
    first, so it is scored LOSS — the conservative reading, because assuming the
    good fill is how backtests lie.
    """
    now = now or int(time.time())
    start_ms = int(row["ts"]) * 1000
    end_ms = min(now, int(row["ts"]) + horizon_hours * 3600) * 1000
    if end_ms <= start_ms:
        return None
    candles = _klines(row["symbol"], start_ms, end_ms)
    if not candles:
        return None

    entry, tp1, sl, side = row["entry"], row["tp1"], row["sl"], row["side"]
    for candle in candles:
        high, low = float(candle[2]), float(candle[3])
        hit_tp = high >= tp1 if side == "BUY" else low <= tp1
        hit_sl = low <= sl if side == "BUY" else high >= sl
        if hit_sl:
            return "LOSS"
        if hit_tp:
            return "WIN"

    expired = now >= int(row["ts"]) + horizon_hours * 3600
    return "EXPIRED" if expired else None


def grade_pending(cfg, now=None):
    horizon = cfg["outcomes"]["horizon_hours"]
    graded = 0
    with db.connect() as conn:
        for row in db.pending_outcomes(conn, horizon, now=now):
            verdict = grade_signal(row, horizon, now=now)
            if verdict:
                db.set_outcome(conn, row["id"], verdict)
                graded += 1
        conn.commit()
    return graded

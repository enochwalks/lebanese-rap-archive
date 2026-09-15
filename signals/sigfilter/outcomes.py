"""Grade past signals so channel trust is earned, not assumed.

Without this the filter is just a pretty regex: every channel stays at the 0.5
prior forever and 'trust' means nothing.

Two free, no-key price sources:
  - crypto  -> Binance public klines
  - metals, forex, indices -> Yahoo Finance chart endpoint

Both give 5-minute high/low candles, which is all the grader needs.
"""

import time

import requests

from . import db

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

# Our normalised symbols -> Yahoo tickers. 6-letter forex pairs get "=X" added
# automatically; everything irregular is listed here.
YAHOO_TICKERS = {
    "XAUUSD": "XAUUSD=X", "XAGUSD": "XAGUSD=X",
    "USOIL": "CL=F",
    "NAS100": "^NDX", "US30": "^DJI", "SPX500": "^GSPC", "GER40": "^GDAXI",
}

GRADEABLE = ("crypto", "metal", "forex", "index")


def _yahoo_ticker(symbol, asset_class):
    if symbol in YAHOO_TICKERS:
        return YAHOO_TICKERS[symbol]
    if asset_class == "forex" and len(symbol) == 6:
        return symbol + "=X"
    return None


def _get(url, params, headers=None, retries=1):
    """One GET with a single retry, returning the response or None."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp
        except requests.RequestException:
            pass
        if attempt < retries:
            time.sleep(0.5)
    return None


def _binance_candles(symbol, start_s, end_s):
    resp = _get(BINANCE_KLINES, {"symbol": symbol, "interval": "5m",
                                 "startTime": start_s * 1000, "endTime": end_s * 1000,
                                 "limit": 1000})
    if not resp:
        return []
    try:
        return [(float(c[2]), float(c[3])) for c in resp.json()]   # (high, low)
    except (ValueError, IndexError, TypeError):
        return []


def _yahoo_candles(ticker, start_s, end_s):
    resp = _get(YAHOO_CHART.format(ticker),
                {"period1": start_s, "period2": end_s, "interval": "5m",
                 "includePrePost": "false"},
                headers={"User-Agent": "Mozilla/5.0"})
    if not resp:
        return []
    try:
        result = resp.json()["chart"]["result"]
        if not result:
            return []
        quote = result[0]["indicators"]["quote"][0]
        highs, lows = quote.get("high") or [], quote.get("low") or []
        return [(float(h), float(l)) for h, l in zip(highs, lows)
                if h is not None and l is not None]
    except (ValueError, KeyError, IndexError, TypeError):
        return []


def candles_for(symbol, asset_class, start_s, end_s):
    """5-minute (high, low) candles from the right source, oldest first."""
    if asset_class == "crypto":
        return _binance_candles(symbol, start_s, end_s)
    ticker = _yahoo_ticker(symbol, asset_class)
    if not ticker:
        return []
    return _yahoo_candles(ticker, start_s, end_s)


def grade_signal(row, horizon_hours=24, now=None):
    """WIN if TP1 trades before SL, LOSS if SL first.

    Returns WIN / LOSS / EXPIRED (neither hit inside the window) / NODATA (past
    the window but no prices available - an unrecognised symbol or a source
    outage) / None (still inside the window, ask again later).

    Within one 5-minute candle that touches both levels we cannot tell which came
    first, so it is scored LOSS - the conservative reading, because assuming the
    good fill is how backtests lie.
    """
    now = now or int(time.time())
    start_s = int(row["ts"])
    horizon_end = start_s + horizon_hours * 3600
    end_s = min(now, horizon_end)
    if end_s <= start_s:
        return None

    candles = candles_for(row["symbol"], row["asset_class"], start_s, end_s)
    past_window = now >= horizon_end
    if not candles:
        return "NODATA" if past_window else None

    entry, tp1, sl, side = row["entry"], row["tp1"], row["sl"], row["side"]
    for high, low in candles:
        hit_tp = high >= tp1 if side == "BUY" else low <= tp1
        hit_sl = low <= sl if side == "BUY" else high >= sl
        if hit_sl:
            return "LOSS"
        if hit_tp:
            return "WIN"

    return "EXPIRED" if past_window else None


def grade_pending(cfg, now=None, polite_delay=0.15):
    horizon = cfg["outcomes"]["horizon_hours"]
    graded = 0
    with db.connect() as conn:
        rows = db.pending_outcomes(conn, horizon, now=now)
        for row in rows:
            verdict = grade_signal(row, horizon, now=now)
            if verdict:
                db.set_outcome(conn, row["id"], verdict)
                graded += 1
            if polite_delay:
                time.sleep(polite_delay)      # don't hammer either free endpoint
        conn.commit()
    return graded

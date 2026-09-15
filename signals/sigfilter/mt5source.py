"""Price history from a locally installed MetaTrader 5 terminal.

Better than any web feed for this job: it is the broker's own bars, at the prices
your orders would actually fill, using the same instrument names the signals use.
Free, no key, no rate limit - it just needs the MT5 terminal installed (and, for
the background task, running).

Windows-only and optional. Every import is guarded so the rest of the tool runs
anywhere; when MT5 is unavailable, callers fall back to the web sources.
"""

from datetime import datetime, timezone

# A few brokers rename the classics. Tried in order after the exact name.
_ALIASES = {
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "GOLDm", "XAU/USD"],
    "XAGUSD": ["XAGUSD", "SILVER", "XAGUSDm"],
    "USOIL": ["USOIL", "WTI", "XTIUSD", "CRUDOIL", "USOILm"],
    "NAS100": ["NAS100", "USTEC", "NDX100", "NAS100.cash", "US100"],
    "US30": ["US30", "DJ30", "US30.cash", "WS30"],
    "SPX500": ["SPX500", "US500", "SP500", "US500.cash"],
    "GER40": ["GER40", "DE40", "DAX40", "GER30"],
}

_mt5_module = "unset"        # cache: the module, or None once we know it's absent
_initialized = None


def _mt5():
    global _mt5_module
    if _mt5_module == "unset":
        try:
            import MetaTrader5 as module
            _mt5_module = module
        except Exception:
            _mt5_module = None
    return _mt5_module


def available():
    """True if the MT5 package is importable and the terminal accepts a connection."""
    global _initialized
    mt5 = _mt5()
    if mt5 is None:
        return False
    if _initialized is None:
        try:
            _initialized = bool(mt5.initialize())
        except Exception:
            _initialized = False
    return _initialized


def _resolve(mt5, symbol):
    """Find the broker's name for our normalised symbol, or None."""
    if mt5.symbol_info(symbol) is not None:
        return symbol
    try:
        names = [s.name for s in (mt5.symbols_get() or [])]
    except Exception:
        return None
    upper = {n.upper(): n for n in names}
    for candidate in _ALIASES.get(symbol, [symbol]):
        cand = candidate.upper()
        if cand in upper:
            return upper[cand]
        for name in names:                       # prefix / contains, e.g. XAUUSD.pro
            up = name.upper()
            if up.startswith(cand) or cand in up:
                return name
    return None


def candles(symbol, start_s, end_s):
    """5-minute (high, low) bars from MT5.

    Returns a list (possibly empty if the symbol exists but has no bars in range),
    or None when MT5 cannot be used at all so the caller falls back to the web.
    """
    if not available():
        return None
    mt5 = _mt5()
    name = _resolve(mt5, symbol)
    if not name:
        return None
    try:
        mt5.symbol_select(name, True)
        rates = mt5.copy_rates_range(
            name, mt5.TIMEFRAME_M5,
            datetime.fromtimestamp(start_s, timezone.utc),
            datetime.fromtimestamp(end_s, timezone.utc),
        )
    except Exception:
        return None
    if rates is None:
        return None
    return [(float(r["high"]), float(r["low"])) for r in rates]

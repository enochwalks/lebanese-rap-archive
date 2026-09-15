"""Symbol normalisation. 'GOLD', 'XAU/USD' and 'xauusd' must collide, or
consensus detection silently never fires."""

import re

_ALIASES = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSD": "XAUUSD", "GOLDUSD": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD",
    "OIL": "USOIL", "CRUDE": "USOIL", "WTI": "USOIL",
    "NAS100": "NAS100", "NASDAQ": "NAS100", "US100": "NAS100",
    "US30": "US30", "DOW": "US30", "DJ30": "US30",
    "SPX500": "SPX500", "US500": "SPX500", "SP500": "SPX500",
    "GER40": "GER40", "DAX": "GER40", "DE40": "GER40",
    "BTC": "BTCUSDT", "XBT": "BTCUSDT", "BITCOIN": "BTCUSDT",
    "ETH": "ETHUSDT", "ETHEREUM": "ETHUSDT",
}

# Lebanese/Gulf channels often name the instrument in Arabic while quoting
# prices in digits, so the latin ticker regex alone would drop them.
_ARABIC = {
    "ذهب": "XAUUSD", "الذهب": "XAUUSD", "دهب": "XAUUSD",
    "فضة": "XAGUSD", "الفضة": "XAGUSD",
    "نفط": "USOIL", "النفط": "USOIL", "بترول": "USOIL",
    "بيتكوين": "BTCUSDT", "البيتكوين": "BTCUSDT", "بتكوين": "BTCUSDT",
    "ايثيريوم": "ETHUSDT", "إيثيريوم": "ETHUSDT", "ايثر": "ETHUSDT",
    "يورو": "EURUSD", "اليورو": "EURUSD",
}

FIAT = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"}

# Liquid coins that may appear bare (no $/# prefix, no quote) and still be a real
# ticker. A word not in here and not prefixed is treated as English, not a coin -
# this is what stops "FUTURES", "ALERT", "SIGNAL" becoming fake USDT pairs.
KNOWN_CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "DOT", "MATIC",
    "LINK", "LTC", "BCH", "TRX", "ATOM", "XLM", "ETC", "FIL", "APT", "ARB",
    "OP", "SUI", "INJ", "TIA", "SEI", "NEAR", "FTM", "ALGO", "AAVE", "UNI",
    "SAND", "MANA", "AXS", "EOS", "XMR", "SHIB", "PEPE", "WIF", "BONK", "RUNE",
    "GALA", "FLOW", "CHZ", "CRV", "LDO", "IMX", "RNDR", "FET", "GRT", "ENS",
    "DYDX", "GMX", "JUP", "WLD", "ORDI", "STX", "TON", "ONDO", "ENA", "NOT",
}
QUOTES = ("USDT", "USDC", "BUSD", "USD", "PERP")

# Longest-first so BTCUSDT wins over BTC when both could match.
_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"([#$]?)"
    r"([A-Za-z]{2,10}[0-9]{0,3})\s*[/\-_]?\s*((?:USDT|USDC|BUSD|USD|EUR|GBP|JPY|CHF|AUD|NZD|CAD)?)"
    r"(?:\s*(?:PERP|PERPETUAL|SPOT))?"
    r"(?![A-Za-z0-9])"
)

# Words that look like tickers but never are.
_STOPWORDS = {
    "BUY", "SELL", "LONG", "SHORT", "ENTRY", "TP", "SL", "TAKE", "STOP", "LOSS",
    "PROFIT", "TARGET", "NOW", "AT", "THE", "AND", "FOR", "SET", "GET", "VIP",
    "FREE", "SIGNAL", "ZONE", "AREA", "PIPS", "LOT", "RISK", "LAYER", "USE",
    "OPEN", "CLOSE", "HIT", "RUN", "PUMP", "LEVERAGE", "CROSS", "ISOLATED",
    "MARKET", "LIMIT", "SCALP", "SWING", "IDEA", "UPDATE", "NEWS", "GOOD",
    "LUCK", "TEAM", "ADMIN", "JOIN", "BOT", "USDT", "USD",
    "FUTURES", "FUTURE", "ALERT", "ALERTS", "RESULT", "RESULTS", "PREMIUM",
    "DAILY", "WEEKLY", "LIVE", "TREND", "BREAK", "SUPPORT", "RESISTANCE",
    "RESIST", "ANALYSIS", "DEPOSIT", "INVEST", "BONUS", "WITHDRAW", "ACCOUNT",
    "CLIENT", "MEMBER", "PRICE", "WAIT", "HOLD", "WATCH", "READY", "SETUP",
    "CHART", "FOREX", "CRYPTO", "STOCK", "STOCKS", "INDEX", "PLAN", "PLANS",
    "DAYS", "WEEK", "CHANNEL", "GROUP", "COPY", "MANAGE", "GOLDEN", "BOOK",
    "SLOT", "CONTACT", "PROFITS", "CAPITAL", "MONEY", "TARGETS", "REACHED",
}


def canonical(raw, allow_bare=True):
    """'btc/usdt' -> 'BTCUSDT'. Returns None if it isn't a plausible instrument.

    allow_bare controls whether an unrecognised bare word (no quote, not a known
    coin) may be assumed to be a USDT pair. extract() passes False unless the
    word carried a $/# prefix or an explicit quote, so ordinary English words
    like "FUTURES" or "ALERT" are not turned into fake tickers.
    """
    if not raw:
        return None
    token = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not token or token in _STOPWORDS:
        return None
    if token in _ALIASES:
        return _ALIASES[token]
    base = token
    for quote in QUOTES:
        if token.endswith(quote) and len(token) > len(quote):
            base = token[: -len(quote)]
            break
    if base in _ALIASES:
        return _ALIASES[base]
    if len(token) == 6 and token[:3] in FIAT and token[3:] in FIAT:
        return token          # EURUSD, GBPJPY...
    if token in FIAT:
        return None
    if len(base) < 2 or len(base) > 10:
        return None
    if token.endswith(("USDT", "USDC", "BUSD")):
        return token
    if token.endswith("USD"):
        return token
    # Bare token, no explicit quote: only a real coin, or a $/#-prefixed word.
    if base in KNOWN_CRYPTO or base in _ALIASES:
        return base + "USDT" if base not in _ALIASES else _ALIASES[base]
    if allow_bare:
        return base + "USDT"
    return None


def extract(text):
    """First plausible instrument mentioned in the message."""
    text = text or ""
    for word, sym in _ARABIC.items():
        if word in text:
            return sym
    for match in _TICKER_RE.finditer(text):
        prefix, base, quote = match.group(1), match.group(2), match.group(3)
        bare = re.sub(r"[0-9]+$", "", base.upper())
        if base.upper() in _STOPWORDS or (bare in _STOPWORDS and base.upper() not in _ALIASES):
            continue
        # A bare word becomes a coin only with positive evidence it is one:
        # a $/# prefix, or an explicit quote (BTC/USDT). Otherwise it must be a
        # known instrument, or it is treated as an English word and skipped.
        allow_bare = bool(prefix) or bool(quote)
        sym = canonical(base + quote, allow_bare=allow_bare)
        if sym:
            return sym
    return None


def asset_class(symbol):
    if not symbol:
        return "unknown"
    if symbol in ("XAUUSD", "XAGUSD"):
        return "metal"
    if symbol in ("USOIL", "NAS100", "US30", "SPX500", "GER40"):
        return "index"
    if len(symbol) == 6 and symbol[:3] in FIAT and symbol[3:] in FIAT:
        return "forex"
    if symbol.endswith(("USDT", "USDC", "BUSD")):
        return "crypto"
    return "unknown"

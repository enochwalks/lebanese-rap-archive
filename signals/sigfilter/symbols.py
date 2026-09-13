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
QUOTES = ("USDT", "USDC", "BUSD", "USD", "PERP")

# Longest-first so BTCUSDT wins over BTC when both could match.
_TICKER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:#|\$)?"
    r"([A-Za-z]{2,10}[0-9]{0,3})\s*[/\-_]?\s*((?:USDT|USDC|BUSD|USD|EUR|GBP|JPY|CHF|AUD|NZD|CAD)?)"
    r"(?:\s*(?:PERP|PERPETUAL|SPOT|FUTURES))?"
    r"(?![A-Za-z0-9])"
)

# Words that look like tickers but never are.
_STOPWORDS = {
    "BUY", "SELL", "LONG", "SHORT", "ENTRY", "TP", "SL", "TAKE", "STOP", "LOSS",
    "PROFIT", "TARGET", "NOW", "AT", "THE", "AND", "FOR", "SET", "GET", "VIP",
    "FREE", "SIGNAL", "ZONE", "AREA", "PIPS", "LOT", "RISK", "LAYER", "USE",
    "OPEN", "CLOSE", "HIT", "RUN", "PUMP", "LEVERAGE", "CROSS", "ISOLATED",
    "MARKET", "LIMIT", "SCALP", "SWING", "IDEA", "UPDATE", "NEWS", "GOOD",
    "LUCK", "TEAM", "ADMIN", "JOIN", "LINK", "BOT", "USDT", "USD",
}


def canonical(raw):
    """'btc/usdt' -> 'BTCUSDT'. Returns None if it isn't a plausible instrument."""
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
    # Bare crypto ticker from a channel that omits the quote: assume USDT pair.
    return base + "USDT"


def extract(text):
    """First plausible instrument mentioned in the message."""
    text = text or ""
    for word, sym in _ARABIC.items():
        if word in text:
            return sym
    for match in _TICKER_RE.finditer(text):
        base, quote = match.group(1), match.group(2)
        bare = re.sub(r"[0-9]+$", "", base.upper())
        if base.upper() in _STOPWORDS or (bare in _STOPWORDS and base.upper() not in _ALIASES):
            continue
        sym = canonical(base + quote)
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

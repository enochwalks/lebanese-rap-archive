"""Turn a free-text Telegram message into a structured signal.

Regex-first and deliberately strict: a message we can't read confidently is
dropped, not guessed at. A wrong parse is worse than no signal, because it
enters the pipeline wearing the same uniform as a real one.
"""

import re
from dataclasses import dataclass, field

from . import symbols

BUY_WORDS = r"(?:buy|long|bullish|call|شراء|صعود|لونق)"
SELL_WORDS = r"(?:sell|short|bearish|put|بيع|هبوط|شورت)"

_SIDE_RE = re.compile(rf"(?<![a-z]){BUY_WORDS}|(?<![a-z]){SELL_WORDS}", re.I | re.U)
_BUY_RE = re.compile(rf"(?<![a-z]){BUY_WORDS}", re.I | re.U)

_NUM = r"([0-9]+(?:[.,][0-9]+)?)"
_RANGE_SEP = r"(?:\s*(?:-|–|—|to|/|\.\.)\s*)"

_ENTRY_RE = re.compile(
    rf"(?:entry|enter|entries|buy\s*zone|sell\s*zone|price|@|الدخول|دخول)\s*"
    rf"(?:zone|point|area|:|=)?\s*{_NUM}(?:{_RANGE_SEP}{_NUM})?",
    re.I | re.U,
)
# The index digit in "TP1" must not be eaten as a price, and "TP 2340" must not
# have its price read as an index. Only treat a digit as an index when a
# separator or a longer number follows it.
_TP_RE = re.compile(
    rf"(?:tp|take\s*profit|targets?|هدف|الاهداف|أهداف)"
    rf"(?:[ ._-]?[0-9](?=\s*[:=@\-–—]|\s+[0-9]{{2,}}))?"
    rf"\s*(?::|=|@)?\s*((?:{_NUM}(?:\s*[,\-–—/|]\s*|\s+)?)+)",
    re.I | re.U,
)
_SL_RE = re.compile(
    rf"(?:sl|s/l|stop\s*-?\s*loss|stoploss|stop|وقف|ستوب)\s*(?::|=|@)?\s*{_NUM}",
    re.I | re.U,
)
# Anchored so "3160" in a stop-loss can never be read as "160x".
_LEV_RE = re.compile(
    r"(?:lev(?:erage)?|cross|isolated)\s*:?\s*x?\s*([0-9]{1,3})\s*x?(?![0-9])"
    r"|(?<![0-9.])([0-9]{1,3})\s?x(?![a-z0-9])"
    r"|(?<![0-9.a-z])x\s?([0-9]{1,3})(?![0-9])",
    re.I,
)

HYPE_WORDS = (
    "guaranteed", "100% win", "no loss", "risk free", "riskfree", "easy money",
    "get rich", "millionaire", "lambo", "moon", "10x your", "double your",
    "last chance", "hurry", "dm me", "send me", "pay ", "subscribe", "vip only",
    "مضمون", "ربح مضمون",
)
_CLOSE_RE = re.compile(
    r"\b(?:closed|close\s+(?:now|half|all)|book(?:ed)?\s+profit|tp\s*[0-9]?\s*(?:hit|reached|done)|"
    r"sl\s*hit|stopped\s*out|cancel(?:led)?|breakeven|be\s*now|move\s+sl)\b",
    re.I,
)


@dataclass
class Signal:
    symbol: str = None
    side: str = None                     # "BUY" | "SELL"
    entries: list = field(default_factory=list)
    tps: list = field(default_factory=list)
    sl: float = None
    leverage: int = None
    asset_class: str = "unknown"
    hype_hits: list = field(default_factory=list)
    parse_notes: list = field(default_factory=list)
    raw: str = ""

    @property
    def entry(self):
        """Mid of the entry zone — the number we actually risk-manage from."""
        if not self.entries:
            return None
        return sum(self.entries) / len(self.entries)

    @property
    def tp1(self):
        if not self.tps:
            return None
        # Nearest target to entry, not whichever the channel listed first.
        ref = self.entry
        if ref is None:
            return self.tps[0]
        return min(self.tps, key=lambda t: abs(t - ref))

    def risk_reward(self):
        entry, tp1, sl = self.entry, self.tp1, self.sl
        if entry is None or tp1 is None or sl is None:
            return None
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        if risk <= 0:
            return None
        return reward / risk

    def is_complete(self):
        return bool(self.symbol and self.side and self.entries and self.tps and self.sl)

    def as_dict(self):
        return {
            "symbol": self.symbol, "side": self.side, "entries": self.entries,
            "tps": self.tps, "sl": self.sl, "leverage": self.leverage,
            "asset_class": self.asset_class, "rr": self.risk_reward(),
        }


def _to_float(token):
    if token is None:
        return None
    token = token.strip()
    # "2,345.50" -> 2345.50 ; "2345,50" -> 2345.50
    if "," in token and "." in token:
        token = token.replace(",", "")
    elif "," in token:
        token = token.replace(",", ".") if len(token.split(",")[-1]) <= 2 else token.replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def _plausible(values, reference):
    """Drop numbers that can't belong to the same instrument as `reference`
    (catches 'join 5000 members', '10x', dates, percentages)."""
    if reference is None:
        return values
    keep = []
    for v in values:
        if v <= 0:
            continue
        ratio = v / reference if reference else 0
        if 0.25 <= ratio <= 4.0:
            keep.append(v)
    return keep


def is_update(text):
    """True for 'TP1 hit', 'close half', 'move SL to BE' — management noise,
    not a new entry. Forwarding these as signals is the classic bug."""
    return bool(_CLOSE_RE.search(text or ""))


def parse(text):
    """Returns a Signal, or None when the message clearly isn't an entry."""
    if not text or not text.strip():
        return None
    if is_update(text):
        return None

    sig = Signal(raw=text)
    flat = re.sub(r"[ \t]+", " ", text)

    side_match = _SIDE_RE.search(flat)
    if not side_match:
        return None
    sig.side = "BUY" if _BUY_RE.match(side_match.group(0)) else "SELL"

    sig.symbol = symbols.extract(flat)
    if not sig.symbol:
        return None
    sig.asset_class = symbols.asset_class(sig.symbol)

    entry_match = _ENTRY_RE.search(flat)
    if entry_match:
        sig.entries = [v for v in (_to_float(entry_match.group(1)), _to_float(entry_match.group(2))) if v]
    else:
        # "XAUUSD SELL 2345" or "sell now 4295 4300" — price(s) right after the
        # side word, optionally a two-number entry zone.
        tail = flat[side_match.end(): side_match.end() + 40]
        loose = re.search(rf"\s*(?:@|at|now\s*@?)?\s*{_NUM}(?:[\s\-–—/]+{_NUM})?", tail)
        if loose:
            values = [_to_float(loose.group(1)), _to_float(loose.group(2))]
            values = [v for v in values if v]
            if values:
                sig.entries = values
                sig.parse_notes.append("entry inferred from price next to side word")

    reference = sig.entries[0] if sig.entries else None

    for tp_match in _TP_RE.finditer(flat):
        for token in re.findall(_NUM, tp_match.group(1)):
            value = _to_float(token)
            if value is not None:
                sig.tps.append(value)
    sig.tps = sorted(set(_plausible(sig.tps, reference)))

    sl_match = _SL_RE.search(flat)
    if sl_match:
        sl = _to_float(sl_match.group(1))
        if sl and (not reference or _plausible([sl], reference)):
            sig.sl = sl

    lev_match = _LEV_RE.search(flat)
    if lev_match:
        raw_lev = lev_match.group(1) or lev_match.group(2) or lev_match.group(3)
        try:
            lev = int(raw_lev)
            sig.leverage = lev if 1 <= lev <= 200 else None
        except (TypeError, ValueError):
            pass

    lowered = flat.lower()
    sig.hype_hits = [w for w in HYPE_WORDS if w in lowered]

    # Direction sanity: a BUY whose stop sits above entry is a broken signal.
    if sig.entry is not None and sig.sl is not None:
        if sig.side == "BUY" and sig.sl >= sig.entry:
            sig.parse_notes.append("stop-loss on wrong side of entry")
            sig.sl = None
        if sig.side == "SELL" and sig.sl <= sig.entry:
            sig.parse_notes.append("stop-loss on wrong side of entry")
            sig.sl = None
    if sig.entry is not None and sig.tps:
        wrong = [t for t in sig.tps if (sig.side == "BUY" and t <= sig.entry) or (sig.side == "SELL" and t >= sig.entry)]
        if wrong:
            sig.tps = [t for t in sig.tps if t not in wrong]
            sig.parse_notes.append("dropped targets on wrong side of entry")

    if not sig.entries and not sig.tps and sig.sl is None:
        return None      # side + ticker only: that's chat, not a signal
    return sig

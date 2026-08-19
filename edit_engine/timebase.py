"""
timebase.py -- exact time arithmetic for a frame-accurate editing engine.

WHY THIS EXISTS
---------------
Float seconds are the single most common source of off-by-one-frame bugs in
homegrown editors. 1/30 is not representable in binary floating point, so
after a few hundred additions a cut that should land on frame 5400 lands on
5399 or 5401, and "the audio drifts" bug reports start. NTSC rates make it
worse: 29.97 is really 30000/1001, and 0.03336670003 is not that number.

So every time value in this engine is an exact rational:

    RationalTime(value, rate)   -- `value` ticks, each tick lasting 1/`rate` s

`value` is usually an integer frame or sample count, `rate` is 30, 30000/1001,
48000, ... Arithmetic is done with fractions.Fraction, so it is exact: no
accumulated drift, ever, no matter how many edits are stacked up.

Mixed-rate arithmetic (video frames + audio samples) promotes to the finer of
the two rates and stays exact. A value that is not an integer at its own rate
is legal on purpose -- that is what gives audio sub-frame precision for J/L
cuts -- and `is_aligned()` / `aligned()` let callers snap when they need to.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Union

Number = Union[int, float, Fraction, "RationalTime"]

# Rounding modes for operations that must land on a whole tick.
FLOOR = "floor"
CEIL = "ceil"
NEAREST = "nearest"
TRUNCATE = "truncate"  # toward zero
_ROUNDINGS = (FLOOR, CEIL, NEAREST, TRUNCATE)

# Rates that carry SMPTE drop-frame timecode.
DROP_FRAME_RATES = {Fraction(30000, 1001): 30, Fraction(60000, 1001): 60}


class TimebaseError(ValueError):
    """Raised for impossible time arithmetic (bad rate, bad rounding, ...)."""


def to_fraction(value: Number) -> Fraction:
    """Coerce to an exact Fraction, mapping common float rates to their real values.

    A user typing 29.97 means 30000/1001; a user typing 23.976 means 24000/1001.
    Silently treating those as literal decimals is how projects end up 3.6
    frames out per hour, so they are snapped here, once, at the boundary.
    """
    if isinstance(value, RationalTime):
        return value.to_seconds()
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TimebaseError(f"non-finite time value: {value!r}")
        for ntsc in (Fraction(24000, 1001), Fraction(30000, 1001),
                     Fraction(48000, 1001), Fraction(60000, 1001),
                     Fraction(120000, 1001)):
            if abs(value - float(ntsc)) < 1e-4:
                return ntsc
        return Fraction(value).limit_denominator(1000000)
    raise TimebaseError(f"cannot interpret {value!r} as a time value")


def normalize_rate(rate: Number) -> Fraction:
    rate = to_fraction(rate)
    if rate <= 0:
        raise TimebaseError(f"rate must be positive, got {rate}")
    return rate


def _round(value: Fraction, rounding: str) -> Fraction:
    if rounding == FLOOR:
        return Fraction(math.floor(value))
    if rounding == CEIL:
        return Fraction(math.ceil(value))
    if rounding == TRUNCATE:
        return Fraction(int(value))
    if rounding == NEAREST:
        # Half away from zero, so a cut exactly between two frames does not
        # silently prefer the even one (banker's rounding surprises editors).
        floor = math.floor(value)
        frac = value - floor
        if frac > Fraction(1, 2):
            return Fraction(floor + 1)
        if frac < Fraction(1, 2):
            return Fraction(floor)
        return Fraction(floor + 1) if value > 0 else Fraction(floor)
    raise TimebaseError(f"unknown rounding mode {rounding!r}; expected one of {_ROUNDINGS}")


@dataclass(frozen=True)
class RationalTime:
    """An exact instant or duration: `value` ticks at `rate` ticks per second."""

    value: Fraction
    rate: Fraction

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", to_fraction(self.value))
        object.__setattr__(self, "rate", normalize_rate(self.rate))

    # -- constructors ----------------------------------------------------

    @classmethod
    def from_seconds(cls, seconds: Number, rate: Number) -> "RationalTime":
        rate = normalize_rate(rate)
        return cls(to_fraction(seconds) * rate, rate)

    @classmethod
    def from_frames(cls, frames: int, rate: Number) -> "RationalTime":
        return cls(Fraction(int(frames)), rate)

    @classmethod
    def zero(cls, rate: Number) -> "RationalTime":
        return cls(Fraction(0), rate)

    # -- conversion ------------------------------------------------------

    def to_seconds(self) -> Fraction:
        return self.value / self.rate

    def to_float_seconds(self) -> float:
        """Only for handing off to the outside world (ffmpeg args, UI labels)."""
        return float(self.to_seconds())

    def rescaled_to(self, rate: Number, rounding: str | None = None) -> "RationalTime":
        """Re-express at another rate. Exact unless a rounding mode is given."""
        rate = normalize_rate(rate)
        if rate == self.rate:
            return self
        value = self.to_seconds() * rate
        if rounding is not None:
            value = _round(value, rounding)
        return RationalTime(value, rate)

    def to_frames(self, rate: Number | None = None, rounding: str = NEAREST) -> int:
        """Whole frame index at `rate` (defaults to this value's own rate)."""
        rate = self.rate if rate is None else normalize_rate(rate)
        return int(_round(self.to_seconds() * rate, rounding))

    # -- alignment -------------------------------------------------------

    def is_aligned(self, rate: Number | None = None) -> bool:
        rate = self.rate if rate is None else normalize_rate(rate)
        return (self.to_seconds() * rate).denominator == 1

    def aligned(self, rate: Number | None = None, rounding: str = NEAREST) -> "RationalTime":
        """Snap onto a whole tick of `rate`, keeping this value's own rate."""
        rate = self.rate if rate is None else normalize_rate(rate)
        snapped_seconds = _round(self.to_seconds() * rate, rounding) / rate
        return RationalTime(snapped_seconds * self.rate, self.rate)

    # -- arithmetic ------------------------------------------------------

    @staticmethod
    def _common_rate(a: "RationalTime", b: "RationalTime") -> Fraction:
        # Promote to the finer grid so nothing is lost; exactness is kept
        # regardless because values stay Fractions.
        return a.rate if a.rate >= b.rate else b.rate

    def __add__(self, other: "RationalTime") -> "RationalTime":
        if not isinstance(other, RationalTime):
            return NotImplemented
        rate = self._common_rate(self, other)
        return RationalTime((self.to_seconds() + other.to_seconds()) * rate, rate)

    def __sub__(self, other: "RationalTime") -> "RationalTime":
        if not isinstance(other, RationalTime):
            return NotImplemented
        rate = self._common_rate(self, other)
        return RationalTime((self.to_seconds() - other.to_seconds()) * rate, rate)

    def __neg__(self) -> "RationalTime":
        return RationalTime(-self.value, self.rate)

    def __abs__(self) -> "RationalTime":
        return RationalTime(abs(self.value), self.rate)

    def __mul__(self, scalar: Number) -> "RationalTime":
        if isinstance(scalar, RationalTime):
            return NotImplemented
        return RationalTime(self.value * to_fraction(scalar), self.rate)

    __rmul__ = __mul__

    def __truediv__(self, other: Number) -> Union["RationalTime", Fraction]:
        """Time / scalar -> time.  Time / time -> dimensionless ratio."""
        if isinstance(other, RationalTime):
            return self.to_seconds() / other.to_seconds()
        divisor = to_fraction(other)
        if divisor == 0:
            raise TimebaseError("division by zero")
        return RationalTime(self.value / divisor, self.rate)

    # -- comparison (rate-independent: compares real time) ----------------

    def _cmp_key(self) -> Fraction:
        return self.to_seconds()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RationalTime):
            return NotImplemented
        return self.to_seconds() == other.to_seconds()

    def __hash__(self) -> int:
        return hash(self.to_seconds())

    def __lt__(self, other: "RationalTime") -> bool:
        return self._cmp_key() < other._cmp_key()

    def __le__(self, other: "RationalTime") -> bool:
        return self._cmp_key() <= other._cmp_key()

    def __gt__(self, other: "RationalTime") -> bool:
        return self._cmp_key() > other._cmp_key()

    def __ge__(self, other: "RationalTime") -> bool:
        return self._cmp_key() >= other._cmp_key()

    def __bool__(self) -> bool:
        return self.value != 0

    # -- timecode --------------------------------------------------------

    def to_timecode(self, rate: Number | None = None, drop_frame: bool | None = None) -> str:
        """SMPTE timecode. Drop-frame is used automatically for 29.97/59.94."""
        rate = self.rate if rate is None else normalize_rate(rate)
        drop = _uses_drop_frame(rate) if drop_frame is None else drop_frame
        frames = self.to_frames(rate, NEAREST)
        return frames_to_timecode(frames, rate, drop)

    @classmethod
    def from_timecode(cls, timecode: str, rate: Number, drop_frame: bool | None = None) -> "RationalTime":
        rate = normalize_rate(rate)
        drop = (";" in timecode or "." in timecode) if drop_frame is None else drop_frame
        return cls(Fraction(timecode_to_frames(timecode, rate, drop)), rate)

    # -- display ---------------------------------------------------------

    def __repr__(self) -> str:
        rate = self.rate
        rate_txt = str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"
        val = self.value
        val_txt = str(val.numerator) if val.denominator == 1 else f"{val.numerator}/{val.denominator}"
        return f"RationalTime({val_txt} @ {rate_txt})"

    def __str__(self) -> str:
        return f"{self.to_float_seconds():.6f}s"


def _uses_drop_frame(rate: Fraction) -> bool:
    return rate in DROP_FRAME_RATES


def frames_to_timecode(frames: int, rate: Number, drop_frame: bool = False) -> str:
    """Frame index -> SMPTE timecode string."""
    rate = normalize_rate(rate)
    negative = frames < 0
    frames = abs(frames)

    if drop_frame:
        if rate not in DROP_FRAME_RATES:
            raise TimebaseError(f"drop-frame timecode is undefined for rate {rate}")
        nominal = DROP_FRAME_RATES[rate]
        # Drop-frame does not drop pictures -- it skips *labels* so that the
        # clock keeps up with 1000/1001 running time. Two labels per minute at
        # 29.97 (four at 59.94), except on every tenth minute.
        drop_per_minute = nominal // 15
        frames_per_10min = nominal * 60 * 10 - drop_per_minute * 9
        frames_per_minute = nominal * 60 - drop_per_minute

        blocks, remainder = divmod(frames, frames_per_10min)
        skipped = drop_per_minute * 9 * blocks
        if remainder >= drop_per_minute:
            skipped += drop_per_minute * ((remainder - drop_per_minute) // frames_per_minute)
        frames += skipped
        nominal_rate = nominal
        sep = ";"
    else:
        nominal_rate = int(math.ceil(float(rate)))
        sep = ":"

    hours, rest = divmod(frames, nominal_rate * 3600)
    minutes, rest = divmod(rest, nominal_rate * 60)
    seconds, frame = divmod(rest, nominal_rate)
    sign = "-" if negative else ""
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{frame:02d}"


_TC_RE = re.compile(r"^(?P<sign>-)?(?P<h>\d+)[:;.](?P<m>\d+)[:;.](?P<s>\d+)[:;.](?P<f>\d+)$")


def timecode_to_frames(timecode: str, rate: Number, drop_frame: bool = False) -> int:
    """SMPTE timecode string -> frame index."""
    rate = normalize_rate(rate)
    match = _TC_RE.match(timecode.strip())
    if not match:
        raise TimebaseError(f"malformed timecode {timecode!r} (want HH:MM:SS:FF)")
    hours, minutes, seconds, frame = (int(match.group(g)) for g in ("h", "m", "s", "f"))

    if drop_frame:
        if rate not in DROP_FRAME_RATES:
            raise TimebaseError(f"drop-frame timecode is undefined for rate {rate}")
        nominal = DROP_FRAME_RATES[rate]
        drop_per_minute = nominal // 15
        if minutes % 10 != 0 and seconds == 0 and frame < drop_per_minute:
            raise TimebaseError(f"{timecode!r} is not a valid drop-frame timecode (that frame is dropped)")
        total_minutes = hours * 60 + minutes
        frames = (hours * 3600 + minutes * 60 + seconds) * nominal + frame
        frames -= drop_per_minute * (total_minutes - total_minutes // 10)
    else:
        nominal = int(math.ceil(float(rate)))
        if frame >= nominal:
            raise TimebaseError(f"{timecode!r} has frame {frame} at rate {rate} (max {nominal - 1})")
        frames = (hours * 3600 + minutes * 60 + seconds) * nominal + frame

    return -frames if match.group("sign") else frames


@dataclass(frozen=True)
class TimeRange:
    """A half-open span [start_time, start_time + duration).

    Half-open is deliberate: a clip occupying frames 0..9 has start 0 and
    duration 10, and butt-joining the next clip at 10 leaves no gap and no
    overlap. Inclusive end points are what produce one-frame gaps between
    "adjacent" clips in hand-rolled editors.
    """

    start_time: RationalTime
    duration: RationalTime

    def __post_init__(self) -> None:
        if self.duration.value < 0:
            raise TimebaseError(f"negative duration: {self.duration!r}")

    # -- constructors ----------------------------------------------------

    @classmethod
    def from_frames(cls, start: int, duration: int, rate: Number) -> "TimeRange":
        return cls(RationalTime.from_frames(start, rate), RationalTime.from_frames(duration, rate))

    @classmethod
    def from_start_end(cls, start: RationalTime, end_exclusive: RationalTime) -> "TimeRange":
        return cls(start, end_exclusive - start)

    # -- edges -----------------------------------------------------------

    @property
    def rate(self) -> Fraction:
        return self.start_time.rate

    @property
    def end_time_exclusive(self) -> RationalTime:
        return self.start_time + self.duration

    def end_time_inclusive(self, rate: Number | None = None) -> RationalTime:
        """Last tick actually occupied -- what a UI shows as the out point."""
        rate = self.rate if rate is None else normalize_rate(rate)
        if self.duration.value == 0:
            return self.start_time
        return self.end_time_exclusive - RationalTime(Fraction(1), rate)

    @property
    def is_empty(self) -> bool:
        return self.duration.value == 0

    # -- predicates ------------------------------------------------------

    def contains(self, other: Union[RationalTime, "TimeRange"]) -> bool:
        if isinstance(other, RationalTime):
            return self.start_time <= other < self.end_time_exclusive
        return (self.start_time <= other.start_time
                and other.end_time_exclusive <= self.end_time_exclusive)

    def overlaps(self, other: "TimeRange") -> bool:
        return (self.start_time < other.end_time_exclusive
                and other.start_time < self.end_time_exclusive)

    def meets(self, other: "TimeRange") -> bool:
        """True when `other` starts exactly where this range ends (butt cut)."""
        return self.end_time_exclusive == other.start_time

    # -- algebra ---------------------------------------------------------

    def intersection(self, other: "TimeRange") -> "TimeRange | None":
        start = max(self.start_time, other.start_time)
        end = min(self.end_time_exclusive, other.end_time_exclusive)
        if end <= start:
            return None
        return TimeRange.from_start_end(start, end)

    def extended_by(self, other: "TimeRange") -> "TimeRange":
        """Smallest range covering both (used to compute sequence extents)."""
        start = min(self.start_time, other.start_time)
        end = max(self.end_time_exclusive, other.end_time_exclusive)
        return TimeRange.from_start_end(start, end)

    def clamped(self, bounds: "TimeRange") -> "TimeRange | None":
        return self.intersection(bounds)

    def shifted(self, offset: RationalTime) -> "TimeRange":
        return TimeRange(self.start_time + offset, self.duration)

    def with_duration(self, duration: RationalTime) -> "TimeRange":
        return TimeRange(self.start_time, duration)

    def with_start(self, start: RationalTime) -> "TimeRange":
        return TimeRange(start, self.duration)

    def rescaled_to(self, rate: Number, rounding: str | None = None) -> "TimeRange":
        return TimeRange(self.start_time.rescaled_to(rate, rounding),
                         self.duration.rescaled_to(rate, rounding))

    def __repr__(self) -> str:
        return (f"TimeRange({self.start_time.to_float_seconds():.4f}s"
                f" +{self.duration.to_float_seconds():.4f}s)")


def span_of(ranges: Iterable[TimeRange], rate: Number) -> TimeRange:
    """Total extent covered by a set of ranges (empty -> zero-length at 0)."""
    total: TimeRange | None = None
    for item in ranges:
        total = item if total is None else total.extended_by(item)
    if total is None:
        return TimeRange(RationalTime.zero(rate), RationalTime.zero(rate))
    return total

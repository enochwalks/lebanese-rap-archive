"""Time math is the foundation; if it drifts, every edit above it drifts."""

from fractions import Fraction

import pytest

from edit_engine.timebase import (CEIL, FLOOR, NEAREST, RationalTime, TimeRange,
                                  TimebaseError, frames_to_timecode,
                                  timecode_to_frames, to_fraction)

NTSC30 = Fraction(30000, 1001)
NTSC24 = Fraction(24000, 1001)


class TestExactness:
    def test_ntsc_frames_never_drift(self):
        """The bug this whole module exists to prevent."""
        one = RationalTime.from_frames(1, NTSC30)
        total = RationalTime.zero(NTSC30)
        for _ in range(200_000):
            total = total + one
        assert total.value == 200_000
        assert total.to_frames() == 200_000

    def test_float_seconds_would_have_drifted(self):
        """Proof the naive approach really does fail, so nobody 'simplifies'
        this module back into floats later."""
        naive = 0.0
        for _ in range(200_000):
            naive += 1001 / 30000
        exact = float(RationalTime.from_frames(200_000, NTSC30).to_seconds())
        assert naive != exact
        assert abs(naive - exact) > 1e-9

    def test_29_97_is_snapped_to_30000_over_1001(self):
        assert to_fraction(29.97) == NTSC30
        assert to_fraction(23.976) == NTSC24

    def test_seconds_are_exact_rationals(self):
        assert RationalTime.from_frames(1, Fraction(30)).to_seconds() == Fraction(1, 30)


class TestArithmetic:
    def test_mixed_rates_promote_to_the_finer_grid(self):
        frame = RationalTime.from_frames(1, Fraction(30))
        sample = RationalTime.from_frames(1, Fraction(48000))
        total = frame + sample
        assert total.rate == 48000
        assert total.to_seconds() == Fraction(1, 30) + Fraction(1, 48000)

    def test_comparison_is_across_rates(self):
        assert RationalTime.from_frames(30, Fraction(30)) == RationalTime.from_frames(48000, Fraction(48000))
        assert RationalTime.from_frames(1, Fraction(30)) > RationalTime.from_frames(1, Fraction(48000))

    def test_division_by_time_gives_a_ratio(self):
        a = RationalTime.from_frames(60, Fraction(30))
        b = RationalTime.from_frames(30, Fraction(30))
        assert a / b == 2

    def test_rescale_is_lossless_when_it_can_be(self):
        t = RationalTime.from_frames(30, Fraction(30))
        assert t.rescaled_to(48000).value == 48000

    def test_rescale_keeps_exactness_without_rounding(self):
        t = RationalTime.from_frames(1, Fraction(30))
        # 1/30 s is 1000/1001 of an NTSC frame -- kept exactly, not rounded away
        assert t.rescaled_to(NTSC30).value == Fraction(1000, 1001)
        assert t.rescaled_to(NTSC30, NEAREST).value == 1

    def test_rounding_modes(self):
        t = RationalTime(Fraction(3, 2), Fraction(1))
        assert t.to_frames(Fraction(1), FLOOR) == 1
        assert t.to_frames(Fraction(1), CEIL) == 2
        assert t.to_frames(Fraction(1), NEAREST) == 2

    def test_alignment_reporting(self):
        assert RationalTime.from_frames(5, Fraction(30)).is_aligned()
        assert not RationalTime(Fraction(1, 2), Fraction(30)).is_aligned()

    def test_bad_rate_is_rejected(self):
        with pytest.raises(TimebaseError):
            RationalTime(Fraction(1), Fraction(0))


class TestTimecode:
    @pytest.mark.parametrize("frames,expected", [
        (0, "00:00:00;00"), (1799, "00:00:59;29"), (1800, "00:01:00;02"),
        (17982, "00:10:00;00"), (107892, "01:00:00;00"),
    ])
    def test_drop_frame_labels(self, frames, expected):
        assert frames_to_timecode(frames, NTSC30, drop_frame=True) == expected

    def test_drop_frame_round_trips_over_an_hour(self):
        for frame in range(0, 110_000, 7):
            tc = frames_to_timecode(frame, NTSC30, drop_frame=True)
            assert timecode_to_frames(tc, NTSC30, drop_frame=True) == frame

    def test_non_drop_round_trips(self):
        for frame in range(0, 90_000, 13):
            tc = frames_to_timecode(frame, Fraction(25))
            assert timecode_to_frames(tc, Fraction(25)) == frame

    def test_drop_frame_is_automatic_for_2997(self):
        assert ";" in RationalTime.from_frames(1800, NTSC30).to_timecode()
        assert ";" not in RationalTime.from_frames(1800, Fraction(30)).to_timecode()

    def test_dropped_labels_are_rejected(self):
        with pytest.raises(TimebaseError):
            timecode_to_frames("00:01:00;00", NTSC30, drop_frame=True)


class TestTimeRange:
    def rate(self):
        return Fraction(30)

    def test_half_open_means_butt_joins_have_no_gap(self):
        a = TimeRange.from_frames(0, 10, self.rate())
        b = TimeRange.from_frames(10, 10, self.rate())
        assert not a.overlaps(b)
        assert a.meets(b)
        assert a.end_time_inclusive().to_frames() == 9

    def test_contains_uses_half_open_semantics(self):
        span = TimeRange.from_frames(10, 10, self.rate())
        assert span.contains(RationalTime.from_frames(10, self.rate()))
        assert not span.contains(RationalTime.from_frames(20, self.rate()))

    def test_intersection(self):
        a = TimeRange.from_frames(0, 20, self.rate())
        b = TimeRange.from_frames(10, 20, self.rate())
        overlap = a.intersection(b)
        assert overlap.start_time.to_frames() == 10
        assert overlap.duration.to_frames() == 10
        assert a.intersection(TimeRange.from_frames(50, 5, self.rate())) is None

    def test_negative_duration_is_rejected(self):
        with pytest.raises(TimebaseError):
            TimeRange(RationalTime.from_frames(0, self.rate()),
                      RationalTime.from_frames(-1, self.rate()))

"""Edit operation semantics.

Each test states the professional behaviour it is pinning down, because these
are the rules an editor's muscle memory depends on. If one of these changes,
someone's cut moves.
"""

from fractions import Fraction

import pytest

from conftest import fake_still_media, fake_video_media
from edit_engine import ops
from edit_engine.commands import CommandError, CommandStack
from edit_engine.model import OverlapError, make_clip
from edit_engine.ops import HEAD, TAIL, LIMIT_LENGTH, LIMIT_MEDIA, LIMIT_NEIGHBOUR
from edit_engine.timebase import TimeRange


@pytest.fixture
def stack(project):
    return CommandStack(project, verify_declarations=True)


def frames(track):
    """Compact view of a track: [(start, duration, source_in)] in frames."""
    return [(c.start.to_frames(), c.duration.to_frames(), c.source_in.to_frames())
            for c in track.clips]


class TestBlade:
    def test_split_is_frame_exact_and_continuous(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        stack.run(ops.Blade(sequence.frames(30), [track.track_id]), sequence)
        assert frames(track) == [(0, 30, 60), (30, 60, 90), (90, 90, 60)]

    def test_blade_on_a_cut_point_does_nothing(self, ab_timeline, stack):
        sequence, track, _, _ = ab_timeline
        before = frames(track)
        stack.run(ops.Blade(sequence.frames(90), [track.track_id]), sequence)
        assert frames(track) == before

    def test_blade_then_delete_left_half(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        new_ids = stack.run(ops.Blade(sequence.frames(30), [track.track_id]), sequence)
        stack.run(ops.RemoveClips([clip_a.clip_id]), sequence)
        assert frames(track) == [(30, 60, 90), (90, 90, 60)]
        assert new_ids


class TestDelete:
    def test_lift_leaves_a_gap(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        stack.run(ops.RemoveClips([clip_a.clip_id]), sequence)
        assert frames(track) == [(90, 90, 60)]
        assert sequence.duration.to_frames() == 180

    def test_ripple_delete_closes_the_gap(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        stack.run(ops.RemoveClips([clip_a.clip_id], ripple=True), sequence)
        assert frames(track) == [(0, 90, 60)]
        assert sequence.duration.to_frames() == 90

    def test_ripple_delete_drags_sync_locked_tracks(self, ab_timeline, stack, project):
        sequence, track, clip_a, _ = ab_timeline
        audio = sequence.audio_tracks[0]
        song = fake_video_media(project, "song.wav", seconds=60)
        audio.place(make_clip(song, sequence, sequence.frames(90),
                              duration=sequence.frames(90), name="sync"))
        stack.run(ops.RemoveClips([clip_a.clip_id], ripple=True), sequence)
        assert frames(audio) == [(0, 90, 0)]

    def test_sync_lock_off_keeps_a_track_still(self, ab_timeline, stack, project):
        sequence, track, clip_a, _ = ab_timeline
        audio = sequence.audio_tracks[0]
        audio.sync_lock = False
        song = fake_video_media(project, "song.wav", seconds=60)
        audio.place(make_clip(song, sequence, sequence.frames(90),
                              duration=sequence.frames(90), name="music"))
        stack.run(ops.RemoveClips([clip_a.clip_id], ripple=True), sequence)
        assert frames(audio) == [(90, 90, 0)]


class TestRangeEdits:
    def test_extract_removes_a_span_and_closes_up(self, ab_timeline, stack):
        sequence, track, _, _ = ab_timeline
        span = TimeRange.from_frames(60, 60, sequence.rate)
        stack.run(ops.ExtractRange(span, [track.track_id]), sequence)
        # A keeps frames 0-59, B's first 30 frames are gone, everything closes up
        assert frames(track) == [(0, 60, 60), (60, 60, 90)]

    def test_lift_of_an_interior_span_splits_the_clip(self, ab_timeline, stack):
        sequence, track, _, _ = ab_timeline
        span = TimeRange.from_frames(30, 30, sequence.rate)
        stack.run(ops.LiftRange(span, [track.track_id]), sequence)
        assert frames(track) == [(0, 30, 60), (60, 30, 120), (90, 90, 60)]


class TestInsertOverwrite:
    def test_overwrite_replaces_and_keeps_length(self, ab_timeline, stack, media):
        sequence, track, _, _ = ab_timeline
        new = make_clip(media, sequence, sequence.frames(60),
                        source_in=sequence.frames(0), duration=sequence.frames(60), name="C")
        stack.run(ops.Overwrite(new, track.track_id), sequence)
        assert frames(track) == [(0, 60, 60), (60, 60, 0), (120, 60, 90)]
        assert sequence.duration.to_frames() == 180

    def test_insert_splits_and_pushes(self, ab_timeline, stack, media):
        sequence, track, _, _ = ab_timeline
        new = make_clip(media, sequence, sequence.frames(30),
                        source_in=sequence.zero(), duration=sequence.frames(45), name="C")
        stack.run(ops.Insert(new, track.track_id), sequence)
        assert frames(track) == [(0, 30, 60), (30, 45, 0), (75, 60, 90), (135, 90, 60)]
        assert sequence.duration.to_frames() == 225

    def test_insert_ripples_sync_locked_audio(self, ab_timeline, stack, project):
        sequence, track, _, _ = ab_timeline
        audio = sequence.audio_tracks[0]
        song = fake_video_media(project, "song.wav", seconds=60)
        audio.place(make_clip(song, sequence, sequence.zero(),
                              duration=sequence.frames(180), name="sync"))
        media = project.registry.get("m-src.mp4")
        new = make_clip(media, sequence, sequence.frames(90),
                        duration=sequence.frames(30), name="C")
        stack.run(ops.Insert(new, track.track_id), sequence)
        # the audio clip is split at the insert point and its tail pushed
        assert frames(audio) == [(0, 90, 0), (120, 90, 90)]


class TestTrim:
    def test_normal_trim_head_opens_a_gap(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        result = stack.run(ops.Trim(clip_b.clip_id, HEAD, sequence.frames(10)), sequence)
        assert result.applied.to_frames() == 10
        assert frames(track) == [(0, 90, 60), (100, 80, 70)]
        assert sequence.duration.to_frames() == 180

    def test_ripple_trim_head_closes_up_and_shortens_the_sequence(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        stack.run(ops.Trim(clip_b.clip_id, HEAD, sequence.frames(10), ripple=True), sequence)
        assert frames(track) == [(0, 90, 60), (90, 80, 70)]
        assert sequence.duration.to_frames() == 170

    def test_ripple_trim_head_backwards_pushes_later_clips(self, ab_timeline, stack, media):
        sequence, track, _, clip_b = ab_timeline
        third = make_clip(media, sequence, sequence.frames(180),
                          source_in=sequence.frames(60), duration=sequence.frames(30), name="C")
        track.place(third)
        stack.run(ops.Trim(clip_b.clip_id, HEAD, -sequence.frames(10), ripple=True), sequence)
        assert frames(track) == [(0, 90, 60), (90, 100, 50), (190, 30, 60)]

    def test_normal_trim_tail_stops_at_the_next_clip(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        result = stack.run(ops.Trim(clip_a.clip_id, TAIL, sequence.frames(20)), sequence)
        assert result.applied.to_frames() == 0
        assert result.limited_by == LIMIT_NEIGHBOUR

    def test_trim_stops_at_the_end_of_the_media(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        # B reads source 60..149 of a 300-frame file: 150 frames of tail handle
        result = stack.run(ops.Trim(clip_b.clip_id, TAIL, sequence.frames(400), ripple=True), sequence)
        assert result.applied.to_frames() == 150
        assert result.limited_by == LIMIT_MEDIA
        assert clip_b.source_out_exclusive.to_frames() == 300

    def test_trim_never_leaves_a_zero_length_clip(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        result = stack.run(ops.Trim(clip_b.clip_id, HEAD, sequence.frames(500)), sequence)
        assert result.limited_by == LIMIT_LENGTH
        assert clip_b.duration.to_frames() == 1

    def test_head_extension_is_limited_by_head_handles(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        result = stack.run(ops.Trim(clip_a.clip_id, HEAD, -sequence.frames(90), ripple=True), sequence)
        assert result.applied.to_frames() == -60      # only 60 frames of handle exist
        assert result.limited_by == LIMIT_MEDIA
        assert clip_a.source_in.to_frames() == 0

    def test_stills_have_unlimited_handles(self, project, sequence, stack):
        still = fake_still_media(project)
        track = sequence.video_tracks[0]
        clip = make_clip(still, sequence, sequence.frames(100), duration=sequence.frames(30))
        track.place(clip)
        result = stack.run(ops.Trim(clip.clip_id, TAIL, sequence.frames(600)), sequence)
        assert result.applied.to_frames() == 600
        assert result.limited_by is None


class TestRoll:
    def test_roll_moves_only_the_cut(self, ab_timeline, stack):
        sequence, track, _, _ = ab_timeline
        result = stack.run(ops.Roll(track.track_id, sequence.frames(90),
                                    sequence.frames(15)), sequence)
        assert result.applied.to_frames() == 15
        assert frames(track) == [(0, 105, 60), (105, 75, 75)]
        assert sequence.duration.to_frames() == 180   # total length is untouched

    def test_roll_is_limited_by_the_incoming_clip_handles(self, ab_timeline, stack):
        sequence, track, clip_a, clip_b = ab_timeline
        result = stack.run(ops.Roll(track.track_id, sequence.frames(90),
                                    -sequence.frames(90)), sequence)
        assert result.applied.to_frames() == -60      # B only has 60 frames of head
        assert result.limited_by == LIMIT_MEDIA

    def test_roll_needs_a_butt_joined_edit(self, ab_timeline, stack):
        sequence, track, _, _ = ab_timeline
        with pytest.raises(CommandError):
            stack.run(ops.Roll(track.track_id, sequence.frames(45), sequence.frames(5)), sequence)


class TestSlipSlide:
    def test_slip_moves_content_only(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        result = stack.run(ops.Slip(clip_b.clip_id, sequence.frames(30)), sequence)
        assert result.applied.to_frames() == 30
        assert frames(track) == [(0, 90, 60), (90, 90, 90)]

    def test_slip_is_limited_by_handles(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        result = stack.run(ops.Slip(clip_a.clip_id, -sequence.frames(90)), sequence)
        assert result.applied.to_frames() == -60
        assert result.limited_by == LIMIT_MEDIA

    def test_slide_moves_the_clip_and_absorbs_into_neighbours(self, ab_timeline, stack, media):
        sequence, track, clip_a, clip_b = ab_timeline
        third = make_clip(media, sequence, sequence.frames(180),
                          source_in=sequence.frames(60), duration=sequence.frames(90), name="C")
        track.place(third)
        result = stack.run(ops.Slide(clip_b.clip_id, sequence.frames(20)), sequence)
        assert result.applied.to_frames() == 20
        assert frames(track) == [(0, 110, 60), (110, 90, 60), (200, 70, 80)]
        assert sequence.duration.to_frames() == 270   # unchanged total

    def test_slide_content_is_untouched(self, ab_timeline, stack, media):
        sequence, track, _, clip_b = ab_timeline
        track.place(make_clip(media, sequence, sequence.frames(180),
                              source_in=sequence.frames(60), duration=sequence.frames(90)))
        source_before = clip_b.source_range
        stack.run(ops.Slide(clip_b.clip_id, sequence.frames(20)), sequence)
        assert clip_b.source_range == source_before


class TestMove:
    def test_move_refuses_to_overlap_by_default(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        with pytest.raises(OverlapError):
            stack.run(ops.MoveClip(clip_a.clip_id, sequence.frames(120)), sequence)
        assert frames(track) == [(0, 90, 60), (90, 90, 60)]   # nothing moved

    def test_move_overwrite_clears_the_destination(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        stack.run(ops.MoveClip(clip_a.clip_id, sequence.frames(120), mode="overwrite"), sequence)
        assert frames(track) == [(90, 30, 60), (120, 90, 60)]

    def test_linked_clips_move_together(self, project, sequence, media, stack):
        video, audio = sequence.video_tracks[0], sequence.audio_tracks[0]
        picture = make_clip(media, sequence, sequence.zero(), duration=sequence.frames(60), name="v")
        sound = make_clip(media, sequence, sequence.zero(), duration=sequence.frames(60), name="a")
        video.place(picture)
        audio.place(sound)
        stack.run(ops.LinkClips([picture.clip_id, sound.clip_id]), sequence)
        stack.run(ops.MoveClip(picture.clip_id, sequence.frames(30)), sequence)
        assert frames(video) == [(30, 60, 0)]
        assert frames(audio) == [(30, 60, 0)]

    def test_locked_tracks_refuse_edits(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        track.locked = True
        with pytest.raises(CommandError):
            stack.run(ops.MoveClip(clip_a.clip_id, sequence.frames(300)), sequence)


class TestSpeed:
    def test_double_speed_halves_the_timeline_duration(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        stack.run(ops.SetSpeed(clip_b.clip_id, Fraction(2)), sequence)
        assert clip_b.duration.to_frames() == 45
        assert clip_b.source_range.duration.to_frames() == 90   # same source shown

    def test_reverse_keeps_duration_and_flips_playback(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        stack.run(ops.SetSpeed(clip_b.clip_id, Fraction(-1)), sequence)
        assert clip_b.duration.to_frames() == 90
        assert clip_b.is_reversed
        # the first timeline frame of a reversed clip is the last source frame
        assert clip_b.source_time_at(clip_b.start).to_frames() == 150

    def test_trimming_a_retimed_clip_consumes_source_at_speed(self, ab_timeline, stack):
        sequence, track, _, clip_b = ab_timeline
        stack.run(ops.SetSpeed(clip_b.clip_id, Fraction(2)), sequence)
        stack.run(ops.Trim(clip_b.clip_id, TAIL, sequence.frames(10), ripple=True), sequence)
        # 10 timeline frames at 2x consume 20 source frames
        assert clip_b.source_range.duration.to_frames() == 110


class TestGaps:
    def test_close_gap(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        stack.run(ops.RemoveClips([clip_a.clip_id]), sequence)
        gap = stack.run(ops.CloseGap(track.track_id, sequence.frames(10)), sequence)
        assert gap.duration.to_frames() == 90
        assert frames(track) == [(0, 90, 60)]

    def test_close_gap_where_there_is_none_is_a_noop(self, ab_timeline, stack):
        sequence, track, _, _ = ab_timeline
        assert stack.run(ops.CloseGap(track.track_id, sequence.frames(10)), sequence) is None

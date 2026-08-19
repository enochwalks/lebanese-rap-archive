"""Render plan compilation -- the timing maths, tested without touching ffmpeg."""

from fractions import Fraction

import pytest

from conftest import fake_audio_media, fake_still_media, fake_video_media
from edit_engine.model import make_clip
from edit_engine.ops import SetSpeed
from edit_engine.render import RenderSettings, compile_plan
from edit_engine.render.ffmpeg import build_command
from edit_engine.render.plan import RenderPlanError
from edit_engine.timebase import TimeRange


@pytest.fixture
def edited(project, sequence, media):
    """red/green/blue style: three one-second clips end to end on V1."""
    track = sequence.video_tracks[0]
    for index in range(3):
        track.place(make_clip(media, sequence, sequence.frames(index * 30),
                              source_in=sequence.frames(index * 60),
                              duration=sequence.frames(30), name=f"C{index}"))
    return sequence


class TestCompilation:
    def test_segments_carry_exact_timeline_positions(self, edited, project):
        plan = compile_plan(edited, project.registry)
        assert [(float(s.timeline_start), float(s.timeline_duration)) for s in plan.video] == \
            [(0.0, 1.0), (1.0, 1.0), (2.0, 1.0)]
        assert plan.frame_count == 90

    def test_source_windows_are_exact(self, edited, project):
        plan = compile_plan(edited, project.registry)
        assert [float(i.source_start) for i in plan.inputs] == [0.0, 2.0, 4.0]

    def test_gaps_do_not_move_later_clips(self, project, sequence, media):
        track = sequence.video_tracks[0]
        track.place(make_clip(media, sequence, sequence.frames(60),
                              duration=sequence.frames(30), name="late"))
        plan = compile_plan(sequence, project.registry)
        assert float(plan.video[0].timeline_start) == 2.0
        assert plan.frame_count == 90

    def test_layers_follow_track_order(self, project, sequence, media):
        bottom, top = sequence.video_tracks[0], sequence.video_tracks[1]
        bottom.place(make_clip(media, sequence, sequence.zero(), duration=sequence.frames(90), name="bg"))
        top.place(make_clip(media, sequence, sequence.frames(30), duration=sequence.frames(30), name="fg"))
        plan = compile_plan(sequence, project.registry)
        layers = {s.clip_name: s.layer for s in plan.video}
        assert layers == {"bg": 0, "fg": 1}

    def test_disabled_tracks_and_clips_are_dropped(self, edited, project):
        edited.video_tracks[0].clips[1].enabled = False
        plan = compile_plan(edited, project.registry)
        assert [s.clip_name for s in plan.video] == ["C0", "C2"]

        edited.video_tracks[0].enabled = False
        plan = compile_plan(edited, project.registry)
        assert plan.video == []

    def test_muted_audio_is_dropped_and_solo_wins(self, project, sequence):
        song = fake_audio_media(project)
        a1, a2 = sequence.audio_tracks
        a1.place(make_clip(song, sequence, sequence.zero(), duration=sequence.frames(60), name="music"))
        a2.place(make_clip(song, sequence, sequence.zero(), duration=sequence.frames(60), name="vo"))
        a1.muted = True
        assert [s.clip_name for s in compile_plan(sequence, project.registry).audio] == ["vo"]
        a1.muted = False
        a1.solo = True
        assert [s.clip_name for s in compile_plan(sequence, project.registry).audio] == ["music"]

    def test_track_and_clip_gain_add_up(self, project, sequence):
        song = fake_audio_media(project)
        track = sequence.audio_tracks[0]
        clip = make_clip(song, sequence, sequence.zero(), duration=sequence.frames(60))
        clip.gain_db = -3.0
        track.volume_db = -3.0
        track.place(clip)
        assert compile_plan(sequence, project.registry).audio[0].gain_db == -6.0

    def test_render_range_clips_without_retiming(self, edited, project):
        span = TimeRange.from_frames(15, 45, edited.rate)
        plan = compile_plan(edited, project.registry, render_range=span)
        assert plan.frame_count == 45
        # first segment is the second half of C0, now sitting at time zero
        first = plan.video[0]
        assert float(first.timeline_start) == 0.0
        assert float(first.timeline_duration) == 0.5
        assert float(plan.inputs[first.input_index].source_start) == 0.5

    def test_in_out_points_drive_the_default_range(self, edited, project):
        edited.in_point = edited.frames(30)
        edited.out_point = edited.frames(60)
        plan = compile_plan(edited, project.registry)
        assert plan.frame_count == 30
        assert [s.clip_name for s in plan.video] == ["C1"]

    def test_retimed_clip_reads_the_right_source_window(self, project, sequence, media, ):
        from edit_engine.commands import CommandStack
        stack = CommandStack(project)
        track = sequence.video_tracks[0]
        clip = make_clip(media, sequence, sequence.zero(), duration=sequence.frames(60), name="fast")
        track.place(clip)
        stack.run(SetSpeed(clip.clip_id, Fraction(2)), sequence)
        plan = compile_plan(sequence, project.registry)
        segment = plan.video[0]
        assert float(segment.timeline_duration) == 1.0          # 30 frames on the timeline
        assert float(plan.inputs[0].source_duration) == 2.0     # 60 frames of source
        assert segment.speed == 2

    def test_reversed_clip_reads_from_the_right_end(self, project, sequence, media):
        from edit_engine.commands import CommandStack
        stack = CommandStack(project)
        track = sequence.video_tracks[0]
        clip = make_clip(media, sequence, sequence.zero(), source_in=sequence.frames(60),
                         duration=sequence.frames(30), name="rev")
        track.place(clip)
        stack.run(SetSpeed(clip.clip_id, Fraction(-1)), sequence)
        plan = compile_plan(sequence, project.registry)
        assert float(plan.inputs[0].source_start) == 2.0
        assert plan.video[0].is_reversed
        assert any("reverse" in w for w in plan.warnings)

    def test_offline_media_fails_before_rendering(self, project, sequence, media):
        track = sequence.video_tracks[0]
        track.place(make_clip(media, sequence, sequence.zero(), duration=sequence.frames(30), name="gone"))
        project.registry.get(media.media_id).info = None
        with pytest.raises(RenderPlanError, match="offline"):
            compile_plan(sequence, project.registry)

    def test_stills_become_looped_inputs(self, project, sequence):
        still = fake_still_media(project)
        sequence.video_tracks[0].place(
            make_clip(still, sequence, sequence.zero(), duration=sequence.frames(90), name="art"))
        plan = compile_plan(sequence, project.registry)
        assert plan.inputs[0].is_still
        command = build_command(plan, "out.mp4")
        assert "-loop" in command

    def test_identical_source_windows_share_one_decoder(self, project, sequence, media):
        track = sequence.video_tracks[0]
        for index in range(4):
            track.place(make_clip(media, sequence, sequence.frames(index * 30),
                                  source_in=sequence.frames(0),
                                  duration=sequence.frames(30), name=f"same{index}"))
        plan = compile_plan(sequence, project.registry)
        assert len(plan.inputs) == 1
        assert len(plan.video) == 4


class TestCommandBuilding:
    def test_graph_uses_one_concat_per_track_not_one_overlay_per_clip(self, project, sequence, media):
        """The performance-critical shape: 60 cuts must not make 60 overlays."""
        track = sequence.video_tracks[0]
        for index in range(60):
            track.place(make_clip(media, sequence, sequence.frames(index * 5),
                                  source_in=sequence.frames(index),
                                  duration=sequence.frames(5), name=f"cut{index}"))
        plan = compile_plan(sequence, project.registry)
        graph = build_command(plan, "out.mp4")[
            build_command(plan, "out.mp4").index("-filter_complex") + 1]
        assert graph.count("overlay") == 0
        assert graph.count("concat=") == 1

    def test_multiple_tracks_use_one_overlay_each(self, project, sequence, media):
        for track in sequence.video_tracks:
            track.place(make_clip(media, sequence, sequence.zero(),
                                  duration=sequence.frames(30), name=track.name))
        plan = compile_plan(sequence, project.registry)
        command = build_command(plan, "out.mp4")
        graph = command[command.index("-filter_complex") + 1]
        assert graph.count("overlay") == 1

    def test_audio_is_delayed_in_samples_not_milliseconds(self, project, sequence):
        song = fake_audio_media(project)
        sequence.audio_tracks[0].place(
            make_clip(song, sequence, sequence.frames(7), duration=sequence.frames(30), name="late"))
        plan = compile_plan(sequence, project.registry)
        command = build_command(plan, "out.mp4")
        graph = command[command.index("-filter_complex") + 1]
        expected = round(7 / 30 * 48000)
        assert f"adelay=delays={expected}S:all=1" in graph

    def test_mix_does_not_normalize(self, project, sequence):
        song = fake_audio_media(project)
        for track in sequence.audio_tracks:
            track.place(make_clip(song, sequence, sequence.zero(),
                                  duration=sequence.frames(30), name=track.name))
        plan = compile_plan(sequence, project.registry)
        command = build_command(plan, "out.mp4")
        graph = command[command.index("-filter_complex") + 1]
        assert "normalize=0" in graph

    def test_silent_timeline_still_gets_an_audio_track(self, project, sequence, media):
        sequence.video_tracks[0].place(
            make_clip(media, sequence, sequence.zero(), duration=sequence.frames(30)))
        plan = compile_plan(sequence, project.registry)
        command = build_command(plan, "out.mp4")
        assert "anullsrc" in command[command.index("-filter_complex") + 1]

    def test_settings_reach_the_command_line(self, edited, project):
        settings = RenderSettings(crf=23, preset="veryfast", audio_bitrate="256k",
                                  extra_output_args=["-metadata", "title=x"])
        plan = compile_plan(edited, project.registry, settings=settings)
        command = build_command(plan, "out.mp4")
        assert "23" in command and "veryfast" in command and "256k" in command
        assert command[-3:] == ["-metadata", "title=x", "out.mp4"]


class TestEdlExport:
    def test_events_carry_source_and_record_timecode(self, edited, project):
        from edit_engine.edl import to_edl
        export = to_edl(edited, project.registry, title="Cut 1")
        assert export.events == 3
        assert "TITLE: Cut 1" in export.text
        assert "FCM: NON-DROP FRAME" in export.text
        # C1 reads source 00:00:02:00-00:00:03:00 and records at 00:00:01:00
        assert "00:00:02:00 00:00:03:00 00:00:01:00 00:00:02:00" in export.text

    def test_upper_video_tracks_are_reported_not_dropped_silently(self, project, sequence, media):
        from edit_engine.edl import to_edl
        for track in sequence.video_tracks:
            track.place(make_clip(media, sequence, sequence.zero(),
                                  duration=sequence.frames(30), name=track.name))
        export = to_edl(sequence, project.registry)
        assert any("one video track" in warning for warning in export.warnings)

    def test_ntsc_sequences_export_drop_frame(self, project):
        from edit_engine.edl import to_edl
        from edit_engine.model import Sequence
        ntsc = Sequence.create(rate=Fraction(30000, 1001))
        project.add_sequence(ntsc)
        assert "FCM: DROP FRAME" in to_edl(ntsc, project.registry).text

"""The beat-edit program: automated cutting, still inspectable and undoable."""

import random
from fractions import Fraction

import pytest

from conftest import fake_audio_media, fake_still_media, fake_video_media
from edit_engine.commands import CommandStack
from edit_engine.model import Project, Sequence
from edit_engine.programs.beat_edit import (BeatEditStyle, build_music_video,
                                            plan_cuts)
from edit_engine.timebase import RationalTime

RATE = Fraction(30)


def seconds(value):
    return RationalTime.from_seconds(value, RATE)


class TestPlanCuts:
    def test_falls_back_to_fixed_intervals_without_beats(self):
        cuts = plan_cuts([], seconds(20), RATE)
        assert [c.to_float_seconds() for c in cuts] == [0.0, 2.5, 5.0, 7.5, 10.0,
                                                        12.5, 15.0, 17.5, 20.0]

    def test_cuts_land_on_beats(self):
        beats = [seconds(i * 0.5) for i in range(1, 60)]
        cuts = plan_cuts(beats, seconds(25), RATE, rng=random.Random(1))
        beat_times = {b.to_float_seconds() for b in beats}
        for cut in cuts[1:-1]:
            assert cut.to_float_seconds() in beat_times

    def test_no_shot_is_shorter_than_the_floor(self):
        beats = [seconds(i * 0.1) for i in range(1, 400)]
        style = BeatEditStyle(min_shot_seconds=1.5, min_beats_per_cut=1, max_beats_per_cut=2)
        cuts = plan_cuts(beats, seconds(30), RATE, style, rng=random.Random(7))
        gaps = [(b - a).to_float_seconds() for a, b in zip(cuts, cuts[1:])]
        assert min(gaps) >= 1.5

    def test_shots_tile_the_song_exactly(self):
        cuts = plan_cuts([], seconds(17.3), RATE)
        assert cuts[0].value == 0
        assert cuts[-1] == seconds(17.3)
        assert cuts == sorted(cuts)

    def test_seeded_runs_are_reproducible(self):
        beats = [seconds(i * 0.4) for i in range(1, 200)]
        first = plan_cuts(beats, seconds(60), RATE, rng=random.Random(99))
        second = plan_cuts(beats, seconds(60), RATE, rng=random.Random(99))
        assert first == second


class TestBuildMusicVideo:
    @pytest.fixture
    def built(self, project):
        sequence = project.add_sequence(Sequence.create(name="MV", rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=30)
        visuals = [fake_video_media(project, f"clip{i}.mp4", seconds=20) for i in range(3)]
        stack = CommandStack(project)
        build_music_video(project, song.path, [v.path for v in visuals],
                          sequence=sequence, stack=stack, seed=4)
        return project, sequence, stack

    def test_song_covers_the_timeline_and_shots_tile_it(self, built):
        project, sequence, _ = built
        video, audio = sequence.video_tracks[0], sequence.audio_tracks[0]
        assert len(audio.clips) == 1
        assert audio.clips[0].duration.to_frames() == 900
        assert video.clips[0].start.value == 0
        assert video.clips[-1].end == audio.clips[0].end
        for first, second in zip(video.clips, video.clips[1:]):
            assert first.end == second.start          # no gaps, no overlaps

    def test_the_timeline_is_valid(self, built):
        project, sequence, _ = built
        assert sequence.validate(project.registry) == []

    def test_every_shot_carries_a_move(self, built):
        _, sequence, _ = built
        assert all(clip.effects for clip in sequence.video_tracks[0].clips)

    def test_the_whole_build_undoes_in_one_step(self, built):
        project, sequence, stack = built
        assert stack.undo_label == "Build Music video"
        stack.undo()
        assert sequence.video_tracks[0].clips == []
        assert sequence.audio_tracks[0].clips == []

    def test_the_same_seed_builds_the_same_edit(self, project):
        def build(seed):
            local = Project(name="x")
            sequence = local.add_sequence(Sequence.create(rate=RATE))
            song = fake_audio_media(local, "song.wav", seconds=30)
            visuals = [fake_video_media(local, f"clip{i}.mp4", seconds=20) for i in range(3)]
            build_music_video(local, song.path, [v.path for v in visuals],
                              sequence=sequence, seed=seed)
            return [(c.start.to_frames(), c.duration.to_frames(), c.media_id,
                     c.effects[0].kind) for c in sequence.video_tracks[0].clips]

        assert build(11) == build(11)
        assert build(11) != build(12)

    def test_an_intro_offsets_everything(self, project):
        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=20)
        visual = fake_video_media(project, "clip.mp4", seconds=20)
        intro = fake_video_media(project, "intro.mp4", seconds=4)
        build_music_video(project, song.path, [visual.path], sequence=sequence,
                          intro_path=intro.path, seed=1)
        video, audio = sequence.video_tracks[0], sequence.audio_tracks[0]
        assert video.clips[0].name == "Intro"
        assert audio.clips[0].start.to_frames() == 120        # song waits for the intro
        assert video.clips[1].start.to_frames() == 120

    def test_stills_get_ken_burns(self, project):
        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=20)
        art = fake_still_media(project, "art.jpg")
        build_music_video(project, song.path, [art.path], sequence=sequence, seed=3)
        kinds = {clip.effects[0].kind for clip in sequence.video_tracks[0].clips}
        assert kinds == {"ken_burns"}

    def test_shots_do_not_repeat_the_same_source_back_to_back(self, project):
        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=120)
        visuals = [fake_video_media(project, f"clip{i}.mp4", seconds=20) for i in range(3)]
        build_music_video(project, song.path, [v.path for v in visuals],
                          sequence=sequence, seed=5)
        clips = sequence.video_tracks[0].clips
        assert all(a.media_id != b.media_id for a, b in zip(clips, clips[1:]))

    def test_shots_never_read_past_the_end_of_their_source(self, project):
        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=90)
        visual = fake_video_media(project, "short.mp4", seconds=6)
        build_music_video(project, song.path, [visual.path], sequence=sequence, seed=8)
        assert sequence.validate(project.registry) == []

    def test_it_needs_a_visual_source(self, project):
        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=20)
        with pytest.raises(ValueError, match="visual source"):
            build_music_video(project, song.path, [], sequence=sequence)

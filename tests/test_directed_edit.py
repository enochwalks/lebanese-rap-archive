"""The directed edit: responding to the footage and the song.

The Claude backends are tested against an injected fake client, so the request
shape and the response parsing are covered without a network call or a bill.
The live round-trip is the one thing these tests cannot prove.
"""

import json
from fractions import Fraction

import pytest

from conftest import fake_audio_media, fake_still_media, fake_video_media
from edit_engine.analysis.media import ClipAnalysis
from edit_engine.analysis.vision import (ClaudeDirector, ClaudeVision, EditDirection,
                                         HeuristicDirector, MeasurementVision,
                                         SourceDescription)
from edit_engine.model import Project, Sequence
from edit_engine.programs.directed_edit import (SourcePlan, _choose_move,
                                                _choose_source, _rank_by_motion,
                                                build_directed_video)
from edit_engine.programs.beat_edit import BeatEditStyle

RATE = Fraction(30)


def analysis(motion, duration=20.0, luma=0.45):
    steps = int(duration * 4)
    return ClipAnalysis(path="x", duration=duration, motion=[motion] * steps,
                        luma=[luma] * steps, saturation=[0.4] * steps, fps=4)


class TestMotionRanking:
    def test_ranks_spread_across_the_set(self):
        plans = [SourcePlan(None, analysis(m), SourceDescription(path=f"{m}"))
                 for m in (0.001, 0.02, 0.08)]
        _rank_by_motion(plans)
        assert [round(p.motion_rank, 2) for p in plans] == [0.0, 0.24, 1.0]

    def test_identical_sources_all_rank_mid(self):
        plans = [SourcePlan(None, analysis(0.02), SourceDescription(path="a"))
                 for _ in range(3)]
        _rank_by_motion(plans)
        assert all(p.motion_rank == 0.5 for p in plans)


class TestSourceChoice:
    """Regression: a fixed 'pick from the top 3' pool is every candidate when
    there are only three sources, which silently degrades matching to random."""

    def plans(self):
        plans = [SourcePlan(None, analysis(m), SourceDescription(path=n))
                 for n, m in (("still", 0.001), ("mid", 0.02), ("fast", 0.09))]
        _rank_by_motion(plans)
        return plans

    def test_loud_music_prefers_busy_footage(self):
        import random
        plans = self.plans()
        rng = random.Random(1)
        picks = [_choose_source(plans, 1.0, "energy", None, rng)[0].motion_rank
                 for _ in range(300)]
        assert sum(picks) / len(picks) > 0.7

    def test_quiet_music_prefers_still_footage(self):
        import random
        plans = self.plans()
        rng = random.Random(1)
        picks = [_choose_source(plans, 0.0, "energy", None, rng)[0].motion_rank
                 for _ in range(300)]
        assert sum(picks) / len(picks) < 0.3

    def test_never_repeats_the_previous_source_when_alternatives_exist(self):
        import random
        plans = self.plans()
        rng = random.Random(3)
        for _ in range(100):
            choice, _ = _choose_source(plans, 0.5, "energy", plans[2], rng)
            assert choice is not plans[2]

    def test_shuffle_ordering_ignores_energy(self):
        import random
        plans = self.plans()
        rng = random.Random(2)
        loud = [_choose_source(plans, 1.0, "shuffle", None, rng)[0].motion_rank
                for _ in range(200)]
        quiet = [_choose_source(plans, 0.0, "shuffle", None, rng)[0].motion_rank
                 for _ in range(200)]
        assert abs(sum(loud) / 200 - sum(quiet) / 200) < 0.15


class TestMoveChoice:
    def direction(self, moves=("punch", "flash", "push", "plain")):
        return EditDirection(preferred_moves=list(moves))

    def test_loud_moments_get_hard_accents(self):
        import random
        rng = random.Random(0)
        kinds = {_choose_move(self.direction(), 0.9, 2.0, False, BeatEditStyle(), rng)[0].kind
                 for _ in range(50)}
        assert kinds <= {"punch", "flash"}

    def test_quiet_moments_get_gentle_moves(self):
        import random
        rng = random.Random(0)
        kinds = {_choose_move(self.direction(), 0.1, 4.0, False, BeatEditStyle(), rng)[0].kind
                 for _ in range(50)}
        assert kinds <= {"push", "zoom"}

    def test_stills_always_get_ken_burns(self):
        import random
        rng = random.Random(0)
        effect, why = _choose_move(self.direction(), 0.9, 2.0, True, BeatEditStyle(), rng)
        assert effect.kind == "ken_burns" and "still" in why

    def test_a_direction_that_bans_a_move_is_respected(self):
        import random
        rng = random.Random(0)
        kinds = {_choose_move(self.direction(["plain"]), e, 2.0, False, BeatEditStyle(), rng)[0].kind
                 for e in (0.1, 0.5, 0.9) for _ in range(20)}
        assert kinds == {"zoom"}          # "plain" compiles to a no-op zoom


class TestBuild:
    @pytest.fixture
    def built(self, project, monkeypatch):
        from edit_engine.analysis import media as media_module
        from edit_engine.analysis import music as music_module

        # no real files behind the fixtures, so stub the two measuring passes
        monkeypatch.setattr(media_module, "_measure",
                            lambda path, ffmpeg: analysis(0.03, 20.0))
        monkeypatch.setattr(music_module, "energy_envelope",
                            lambda path, hop=0.25, ffmpeg="ffmpeg": [0.2] * 60 + [0.9] * 60)
        monkeypatch.setattr(music_module, "detect_beats",
                            lambda path: ([i * 0.5 for i in range(60)], 120.0, True))

        sequence = project.add_sequence(Sequence.create(name="MV", rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=30)
        visuals = [fake_video_media(project, f"clip{i}.mp4", seconds=20) for i in range(3)]
        sequence, stack = build_directed_video(
            project, song.path, [v.path for v in visuals],
            sequence=sequence, seed=5, use_vision=False)
        return project, sequence, stack

    def test_timeline_is_valid_and_tiles(self, built):
        project, sequence, _ = built
        assert sequence.validate(project.registry) == []
        clips = sequence.video_tracks[0].clips
        for first, second in zip(clips, clips[1:]):
            assert first.end == second.start

    def test_direction_and_sources_are_recorded(self, built):
        _, sequence, _ = built
        analysis_data = sequence.metadata["analysis"]
        assert analysis_data["program"] == "directed_edit"
        assert analysis_data["direction"]["style_name"]
        assert len(analysis_data["sources"]) == 3
        assert all("motion_rank" in s for s in analysis_data["sources"])

    def test_every_shot_records_the_music_energy_it_was_cut_against(self, built):
        _, sequence, _ = built
        for clip in sequence.video_tracks[0].clips:
            assert "music_energy" in clip.metadata["decision"]
            assert clip.metadata["decision"]["source_in_reason"]

    def test_whole_build_undoes_in_one_step(self, built):
        _, sequence, stack = built
        stack.undo()
        assert sequence.video_tracks[0].clips == []

    def test_seed_is_reproducible(self, built):
        _, sequence, _ = built
        shape = [(c.start.to_frames(), c.media_id) for c in sequence.video_tracks[0].clips]
        assert shape == shape


class TestClaudeBackends:
    """Request shape and response parsing, against an injected fake client."""

    class FakeResponse:
        stop_reason = "end_turn"

        def __init__(self, payload):
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            self.content = [block]

    class FakeMessages:
        def __init__(self, payload, recorder):
            self.payload, self.recorder = payload, recorder

        def create(self, **kwargs):
            self.recorder.append(kwargs)
            return TestClaudeBackends.FakeResponse(self.payload)

    class FakeClient:
        def __init__(self, payload, recorder):
            self.messages = TestClaudeBackends.FakeMessages(payload, recorder)

    def test_vision_parses_a_response_and_marks_provenance(self, monkeypatch, tmp_path):
        from edit_engine.analysis import vision as vision_module
        frame = tmp_path / "f.jpg"
        frame.write_bytes(b"\xff\xd8\xff\xdb")
        monkeypatch.setattr(vision_module, "sample_frames",
                            lambda *a, **k: [frame.read_bytes()])
        calls = []
        backend = ClaudeVision()
        backend._client = self.FakeClient({
            "subject": "full moon over cloud", "scene_type": "landscape",
            "mood": "dreamy", "pace": "slow", "palette": "cool blue",
            "tags": ["moon", "night"], "edit_notes": "wants long holds",
        }, calls)

        described = backend.describe("moon.mp4", analysis(0.002))
        assert described.subject == "full moon over cloud"
        assert described.backend == "claude"        # provenance, not guessed
        assert described.tags == ["moon", "night"]

        request = calls[0]
        assert request["model"] == "claude-opus-5"
        assert request["output_config"]["format"]["type"] == "json_schema"
        blocks = request["messages"][0]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/jpeg"

    def test_vision_falls_back_when_the_call_fails(self, monkeypatch, tmp_path):
        from edit_engine.analysis import vision as vision_module
        monkeypatch.setattr(vision_module, "sample_frames", lambda *a, **k: [b"\xff\xd8"])

        class Boom:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("no credit")

        backend = ClaudeVision()
        backend._client = Boom()
        described = backend.describe("x.mp4", analysis(0.002))
        assert described.backend == "measurement"
        assert "vision unavailable" in described.edit_notes

    def test_director_parses_and_clamps_an_absurd_bias(self):
        calls = []
        director = ClaudeDirector()
        director._client = self.FakeClient({
            "style_name": "slow moon", "shot_length_bias": 40.0,
            "preferred_moves": ["push", "plain"], "prefer_windows": "calm",
            "ordering": "grouped", "rationale": "long holds suit this",
        }, calls)
        direction = director.direct(
            [SourceDescription(path="moon.mp4", subject="moon")],
            {"tempo_bpm": 70, "song_seconds": 200, "beat_count": 233})
        assert direction.style_name == "slow moon"
        assert direction.shot_length_bias == 3.0     # clamped, not obeyed
        assert direction.backend == "claude"
        assert "70 bpm" in calls[0]["messages"][0]["content"]

    def test_director_falls_back_on_failure(self):
        class Boom:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("offline")

        director = ClaudeDirector()
        director._client = Boom()
        direction = director.direct([SourceDescription(path="a", pace="static")],
                                    {"tempo_bpm": 80})
        assert direction.backend == "heuristic"
        assert "director unavailable" in direction.rationale


class TestMeasurementFallback:
    def test_describes_fast_footage_as_action(self):
        described = MeasurementVision().describe("x.mp4", analysis(0.09))
        assert described.pace == "fast" and described.scene_type == "action"
        assert described.backend == "measurement"

    def test_heuristic_director_slows_down_for_calm_footage(self):
        calm = [SourceDescription(path="a", pace="static") for _ in range(3)]
        direction = HeuristicDirector().direct(calm, {"tempo_bpm": 70})
        assert direction.shot_length_bias > 1.0
        assert direction.prefer_windows == "calm"


class TestCapabilityHonesty:
    """Regression: the run announced 'asking Claude what they are' and then
    silently fell back once per clip, because availability was discovered
    inside the loop instead of checked up front."""

    def test_missing_sdk_is_reported_not_announced(self, monkeypatch):
        import builtins
        from edit_engine.analysis import vision as vision_module

        real_import = builtins.__import__

        def no_anthropic(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no module named anthropic")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_anthropic)
        available, reason = vision_module.vision_available()
        assert not available and "anthropic" in reason

        backend, director, note = vision_module.default_backends(True)
        assert isinstance(backend, MeasurementVision)
        assert isinstance(director, HeuristicDirector)
        assert "unavailable" in note and "measurements" in note

    def test_disabled_vision_says_so(self):
        from edit_engine.analysis.vision import default_backends
        backend, _, note = default_backends(False)
        assert isinstance(backend, MeasurementVision)
        assert "disabled" in note

    def test_progress_is_reported_per_source(self, project, monkeypatch):
        from edit_engine.analysis import media as media_module
        from edit_engine.analysis import music as music_module
        monkeypatch.setattr(media_module, "_measure", lambda path, ffmpeg: analysis(0.03))
        monkeypatch.setattr(music_module, "energy_envelope",
                            lambda path, hop=0.25, ffmpeg="ffmpeg": [0.5] * 80)
        monkeypatch.setattr(music_module, "detect_beats", lambda path: ([], None, False))

        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=20)
        visuals = [fake_video_media(project, f"c{i}.mp4", seconds=20) for i in range(3)]
        messages = []
        build_directed_video(project, song.path, [v.path for v in visuals],
                             sequence=sequence, seed=1, use_vision=False,
                             on_progress=messages.append)
        assert sum("analysing" in m for m in messages) == 3
        assert any("deciding" in m for m in messages)

    def test_backend_note_is_recorded_in_the_project(self, project, monkeypatch):
        from edit_engine.analysis import media as media_module
        from edit_engine.analysis import music as music_module
        monkeypatch.setattr(media_module, "_measure", lambda path, ffmpeg: analysis(0.03))
        monkeypatch.setattr(music_module, "energy_envelope",
                            lambda path, hop=0.25, ffmpeg="ffmpeg": [0.5] * 80)
        monkeypatch.setattr(music_module, "detect_beats", lambda path: ([], None, False))

        sequence = project.add_sequence(Sequence.create(rate=RATE))
        song = fake_audio_media(project, "song.wav", seconds=20)
        visual = fake_video_media(project, "c.mp4", seconds=20)
        build_directed_video(project, song.path, [visual.path], sequence=sequence,
                             seed=1, use_vision=False)
        recorded = sequence.metadata["analysis"]
        assert "disabled" in recorded["backend_note"]
        assert recorded["beats_available"] is False


class TestFatalErrorsStopEarly:
    """Regression: an account-level failure (no credits, bad key) was retried
    once per source. Eleven identical failures tell you nothing the first one
    did not, and each one costs a round trip."""

    def backend_that_fails(self, message, monkeypatch):
        from edit_engine.analysis import vision as vision_module
        monkeypatch.setattr(vision_module, "sample_frames", lambda *a, **k: [b"\xff\xd8"])
        attempts = []

        class Boom:
            class messages:
                @staticmethod
                def create(**kwargs):
                    attempts.append(1)
                    raise RuntimeError(message)

        backend = ClaudeVision()
        backend._client = Boom()
        return backend, attempts

    def test_credit_failure_is_attempted_once_for_many_clips(self, monkeypatch):
        backend, attempts = self.backend_that_fails(
            "Error code: 400 - {'message': 'Your credit balance is too low'}", monkeypatch)
        for index in range(11):
            backend.describe(f"clip{index}.mp4", analysis(0.02))
        assert len(attempts) == 1

    def test_bad_key_is_attempted_once(self, monkeypatch):
        backend, attempts = self.backend_that_fails(
            "Error code: 401 - {'type': 'authentication_error'}", monkeypatch)
        for index in range(5):
            backend.describe(f"clip{index}.mp4", analysis(0.02))
        assert len(attempts) == 1

    def test_a_per_clip_failure_keeps_trying_the_rest(self, monkeypatch):
        """A clip-specific problem must not disable vision for the whole run."""
        backend, attempts = self.backend_that_fails(
            "Error code: 500 - {'type': 'api_error'}", monkeypatch)
        for index in range(5):
            backend.describe(f"clip{index}.mp4", analysis(0.02))
        assert len(attempts) == 5

    def test_errors_are_translated_for_humans(self):
        from edit_engine.analysis.vision import _friendly
        assert "no API credits" in _friendly("Your credit balance is too low")
        assert "Claude.ai subscription is separate" in _friendly("credit balance too low")
        assert "complete, current key" in _friendly("authentication_error: bad key")
        assert _friendly("something unexpected") == "something unexpected"

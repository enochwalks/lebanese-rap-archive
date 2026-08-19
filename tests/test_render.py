"""End-to-end render verification against real ffmpeg output.

These are the tests that prove the engine is frame-accurate rather than
approximately accurate: they render a timeline of solid-colour sources and
then read individual pixels out of the encoded file to check that each cut
landed on the exact frame the model said it would. Audio is checked the same
way, by measuring level inside windows that should be silent.

Skipped automatically when ffmpeg is not installed.
"""

import array
import subprocess
from fractions import Fraction

import pytest

from conftest import requires_ffmpeg
from edit_engine.model import Project, Sequence, make_clip
from edit_engine.render import FFmpegRenderer, RenderSettings, compile_plan
from edit_engine.render.ffmpeg import build_command
from edit_engine.render.ffmpeg import RenderError

WIDTH, HEIGHT, FPS = 320, 180, Fraction(30)


def fast():
    """Fresh settings per render -- a shared instance is mutable state that
    one test can leak into the next."""
    return RenderSettings(preset="ultrafast", crf=24)

pytestmark = requires_ffmpeg


def make_source(directory, name, color, frequency, seconds=5):
    path = directory / f"{name}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True)
    return path


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("sources")
    return {
        "red": make_source(directory, "red", "red", 220),
        "green": make_source(directory, "green", "green", 440),
        "blue": make_source(directory, "blue", "blue", 880),
        "silent": make_source(directory, "silent", "black", 0),
    }


def frame_color(path, index):
    """Centre pixel of one decoded frame, as (r, g, b)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select='eq(n\\,{index})'",
         "-vsync", "0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    offset = (HEIGHT // 2) * WIDTH * 3 + (WIDTH // 2) * 3
    return tuple(raw[offset:offset + 3])


def dominant(rgb):
    red, green, blue = rgb
    if red > 120 and green < 90 and blue < 90:
        return "red"
    if green > 90 and red < 90 and blue < 90:
        return "green"
    if blue > 120 and red < 90 and green < 90:
        return "blue"
    if max(rgb) < 40:
        return "black"
    return f"other{rgb}"


def rms(path, start, end):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
         "-ar", "48000", "-"], capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    window = samples[int(start * 48000):int(end * 48000)]
    if not window:
        return 0.0
    return (sum(value * value for value in window) / len(window)) ** 0.5


def probe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "default=nw=1:nk=1",
         str(path)], capture_output=True, text=True, check=True).stdout.strip().splitlines()


@pytest.fixture
def project_with_sources(sources):
    project = Project(name="render-test")
    sequence = Sequence.create(name="Main", rate=FPS, width=WIDTH, height=HEIGHT,
                               video_tracks=2, audio_tracks=2)
    project.add_sequence(sequence)
    media = {name: project.import_media(path) for name, path in sources.items()}
    return project, sequence, media


class TestFrameAccuracy:
    def test_cuts_land_on_the_exact_frame(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        track = sequence.video_tracks[0]
        for index, name in enumerate(("red", "green", "blue")):
            track.place(make_clip(media[name], sequence, sequence.frames(index * 30),
                                  duration=sequence.frames(30), name=name))
        output = tmp_path / "cuts.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)

        assert probe(output, "stream=nb_frames")[0] == "90"
        assert [dominant(frame_color(output, n)) for n in (0, 29, 30, 59, 60, 89)] == \
            ["red", "red", "green", "green", "blue", "blue"]

    def test_gaps_render_as_background(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        track = sequence.video_tracks[0]
        track.place(make_clip(media["red"], sequence, sequence.zero(),
                              duration=sequence.frames(30), name="a"))
        track.place(make_clip(media["blue"], sequence, sequence.frames(60),
                              duration=sequence.frames(30), name="b"))
        output = tmp_path / "gap.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)

        assert [dominant(frame_color(output, n)) for n in (0, 29, 30, 59, 60, 89)] == \
            ["red", "red", "black", "black", "blue", "blue"]

    def test_upper_track_composites_over_lower(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        sequence.video_tracks[0].place(
            make_clip(media["red"], sequence, sequence.zero(),
                      duration=sequence.frames(90), name="bg"))
        sequence.video_tracks[1].place(
            make_clip(media["green"], sequence, sequence.frames(30),
                      duration=sequence.frames(30), name="fg"))
        output = tmp_path / "layers.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)

        assert [dominant(frame_color(output, n)) for n in (0, 29, 30, 59, 60, 89)] == \
            ["red", "red", "green", "green", "red", "red"]

    def test_trimmed_source_reads_the_requested_frames(self, project_with_sources, tmp_path):
        """A clip reading source 2.0s-3.0s must show that second, not the head."""
        project, sequence, media = project_with_sources
        marked = tmp_path / "marked.mp4"
        # red for 2s then blue for 3s, so the source window is identifiable
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", f"color=c=red:s={WIDTH}x{HEIGHT}:r={FPS}:d=2",
             "-f", "lavfi", "-i", f"color=c=blue:s={WIDTH}x{HEIGHT}:r={FPS}:d=3",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(marked)], check=True)
        source = project.import_media(marked)
        sequence.video_tracks[0].place(
            make_clip(source, sequence, sequence.zero(), source_in=sequence.frames(60),
                      duration=sequence.frames(30), name="second-half"))
        output = tmp_path / "trimmed.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)
        assert dominant(frame_color(output, 0)) == "blue"
        assert dominant(frame_color(output, 29)) == "blue"


class TestAudioAccuracy:
    def test_audio_lands_at_its_timeline_position(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        sequence.video_tracks[0].place(
            make_clip(media["red"], sequence, sequence.zero(),
                      duration=sequence.frames(90), name="picture"))
        sequence.audio_tracks[0].place(
            make_clip(media["green"], sequence, sequence.frames(60),
                      duration=sequence.frames(30), name="late-sound"))
        output = tmp_path / "audio.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)

        assert rms(output, 0.1, 1.8) < 50           # silent before the clip
        assert rms(output, 2.1, 2.9) > 500          # sounding after it

    def test_gain_is_applied(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        loud = make_clip(media["green"], sequence, sequence.zero(),
                         duration=sequence.frames(30), name="loud")
        sequence.audio_tracks[0].place(loud)
        output_loud = tmp_path / "loud.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output_loud)

        loud.gain_db = -20.0
        output_quiet = tmp_path / "quiet.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output_quiet)

        assert rms(output_quiet, 0.1, 0.9) < rms(output_loud, 0.1, 0.9) / 5

    def test_two_audio_tracks_mix(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        for track, name in zip(sequence.audio_tracks, ("green", "blue")):
            track.place(make_clip(media[name], sequence, sequence.zero(),
                                  duration=sequence.frames(30), name=name))
        output = tmp_path / "mix.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)
        assert rms(output, 0.1, 0.9) > 500


class TestRenderMechanics:
    def test_render_range_only_renders_the_work_area(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        track = sequence.video_tracks[0]
        for index, name in enumerate(("red", "green", "blue")):
            track.place(make_clip(media[name], sequence, sequence.frames(index * 30),
                                  duration=sequence.frames(30), name=name))
        sequence.in_point = sequence.frames(30)
        sequence.out_point = sequence.frames(60)
        output = tmp_path / "work-area.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)
        assert probe(output, "stream=nb_frames")[0] == "30"
        assert dominant(frame_color(output, 0)) == "green"

    def test_a_failed_render_leaves_no_output_file(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        sequence.video_tracks[0].place(
            make_clip(media["red"], sequence, sequence.zero(),
                      duration=sequence.frames(30), name="a"))
        plan = compile_plan(sequence, project.registry, settings=fast())
        plan.settings.extra_output_args = ["-c:v", "definitely-not-a-codec"]
        output = tmp_path / "broken.mp4"
        with pytest.raises(RenderError):
            FFmpegRenderer().render(plan, output)
        assert not output.exists()
        assert not list(tmp_path.glob(".broken-*"))     # temp file cleaned up too

    def test_dry_run_builds_a_command_without_rendering(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        sequence.video_tracks[0].place(
            make_clip(media["red"], sequence, sequence.zero(),
                      duration=sequence.frames(30), name="a"))
        output = tmp_path / "never-written.mp4"
        report = FFmpegRenderer().render(
            compile_plan(sequence, project.registry, settings=fast()), output, dry_run=True)
        assert not output.exists()
        assert report.command[0].endswith("ffmpeg")

    def test_progress_is_reported(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        sequence.video_tracks[0].place(
            make_clip(media["red"], sequence, sequence.zero(),
                      duration=sequence.frames(60), name="a"))
        seen = []
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()),
                                tmp_path / "progress.mp4",
                                progress=lambda done, total: seen.append(done))
        assert seen and seen[-1] == pytest.approx(2.0)


class TestRetimeRendering:
    def test_double_speed_halves_the_output(self, project_with_sources, tmp_path):
        project, sequence, media = project_with_sources
        from edit_engine.commands import CommandStack
        from edit_engine.ops import SetSpeed
        stack = CommandStack(project)
        clip = make_clip(media["red"], sequence, sequence.zero(),
                         duration=sequence.frames(60), name="fast")
        sequence.video_tracks[0].place(clip)
        stack.run(SetSpeed(clip.clip_id, Fraction(2)), sequence)
        output = tmp_path / "fast.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)
        assert probe(output, "stream=nb_frames")[0] == "30"


class TestEffectsDoNotFreezeFootage:
    """Regression: `ken_burns` on video froze the shot on its first frame.

    zoompan's `d` is output frames PER INPUT FRAME. d=N on a still is a
    correct N-frame move; d=N on video generates N frames from input frame 0,
    so the picture stops while the camera drifts. The shot picker was choosing
    that treatment for most long video shots, which read as the editor
    randomly pausing the video.
    """

    @pytest.fixture
    def colour_phases(self, tmp_path):
        """One second each of red, green, blue -- so 'did the content advance?'
        is a question about hue, which a zoom cannot fake."""
        path = tmp_path / "phases.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", f"color=c=red:s={WIDTH}x{HEIGHT}:r={FPS}:d=1",
             "-f", "lavfi", "-i", f"color=c=green:s={WIDTH}x{HEIGHT}:r={FPS}:d=1",
             "-f", "lavfi", "-i", f"color=c=blue:s={WIDTH}x{HEIGHT}:r={FPS}:d=1",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    def render_with(self, project, sequence, media, kind, output):
        from edit_engine.model import Effect, new_id
        clip = make_clip(media, sequence, sequence.zero(),
                         duration=sequence.frames(90), name="shot")
        if kind:
            clip.effects = [Effect(new_id("fx"), kind, {})]
        sequence.video_tracks[0].place(clip)
        FFmpegRenderer().render(
            compile_plan(sequence, project.registry, settings=fast()), output)

    @pytest.mark.parametrize("kind", [None, "punch", "push", "ken_burns", "zoom", "flash"])
    def test_video_keeps_playing_under_every_effect(self, project_with_sources,
                                                    colour_phases, tmp_path, kind):
        project, sequence, _ = project_with_sources
        media = project.import_media(colour_phases)
        output = tmp_path / f"eff-{kind}.mp4"
        self.render_with(project, sequence, media, kind, output)
        seen = [dominant(frame_color(output, n)) for n in (5, 45, 85)]
        assert seen == ["red", "green", "blue"], (
            f"{kind} froze the footage: sampled {seen}")

    def test_ken_burns_still_gets_the_real_move_on_a_photo(self, project_with_sources,
                                                          tmp_path):
        """The photo path must keep using d=frames, or stills stop moving."""
        from edit_engine.model import Effect, new_id
        project, sequence, _ = project_with_sources
        photo = tmp_path / "photo.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"testsrc2=size={WIDTH * 2}x{HEIGHT * 2}:d=1",
                        "-frames:v", "1", str(photo)], check=True)
        media = project.import_media(photo)
        clip = make_clip(media, sequence, sequence.zero(),
                         duration=sequence.frames(60), name="still")
        clip.effects = [Effect(new_id("fx"), "ken_burns", {"motion": "pan_lr"})]
        sequence.video_tracks[0].place(clip)
        plan = compile_plan(sequence, project.registry, settings=fast())
        command = build_command(plan, tmp_path / "kb.mp4")
        graph = command[command.index("-filter_complex") + 1]
        assert "zoompan" in graph and ":d=60:" in graph, "stills lost their Ken Burns move"

    def test_a_still_actually_moves_when_rendered(self, project_with_sources, tmp_path):
        from edit_engine.model import Effect, new_id
        project, sequence, _ = project_with_sources
        photo = tmp_path / "photo2.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"testsrc2=size={WIDTH * 2}x{HEIGHT * 2}:d=1",
                        "-frames:v", "1", str(photo)], check=True)
        media = project.import_media(photo)
        clip = make_clip(media, sequence, sequence.zero(),
                         duration=sequence.frames(45), name="still")
        clip.effects = [Effect(new_id("fx"), "ken_burns", {"motion": "pan_lr"})]
        sequence.video_tracks[0].place(clip)
        output = tmp_path / "kb-render.mp4"
        FFmpegRenderer().render(compile_plan(sequence, project.registry, settings=fast()), output)
        first, last = frame_color(output, 2), frame_color(output, 42)
        assert first != last, "Ken Burns produced a static picture"

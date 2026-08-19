"""
beat_edit.py -- build a beat-synced music-video timeline.

This is the automation the Lebanese Rap Archive channel actually needs,
rewritten as an *edit program*: it analyses the song, decides where the cuts
go, and then performs real edits on a real timeline. The result is a Sequence.

Why that is worth the indirection versus the older approach of shelling out to
ffmpeg once per shot:

  * one render, not one process per cut. A 3-minute song at ~2.5s shots is
    ~70 encodes, ~70 temp files and a concat pass in the old shape; here it is
    a single filtergraph.
  * the cut list is data. You can print it, export an EDL, open it in Resolve,
    move three cuts by hand, and render the fixed version.
  * every decision is undoable, so "re-roll the last 20 shots" is `undo`
    rather than "delete the output and run the whole thing again".
  * the shot pattern is reproducible: pass a seed and you get the same cut.

What Phase 1 does not do yet: the audio-reactive waveform/EQ overlay and the
lower-third text from the old `video_builder.combo` style. Those are
sequence-level generated layers rather than clip effects, and they arrive with
the generator-track work in a later phase. Until then `video_builder.py` still
owns that look and is unchanged.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, List, Optional, Sequence as Seq, Tuple

from .. import ops
from ..commands import CommandStack
from ..media import MediaRef
from ..model import Effect, Project, Sequence, Track, make_clip, new_id
from ..timebase import NEAREST, RationalTime

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
#: formats ffmpeg usually cannot open without extra libraries. Reported rather
#: than silently skipped -- "why is half my artwork missing" is a bad afternoon.
UNREADABLE_SUFFIXES = {".heic", ".heif", ".avif"}


@dataclass
class BeatEditStyle:
    """The look of the cut. Defaults match the channel's existing edit."""

    min_beats_per_cut: int = 4
    max_beats_per_cut: int = 8
    min_shot_seconds: float = 1.5
    fallback_shot_seconds: float = 2.5
    #: how often a cut gets a white-flash accent
    flash_probability: float = 0.16
    #: shots longer than this lean towards a slow push instead of a punch
    long_shot_seconds: float = 3.5
    punch_amount: float = 0.12
    ken_burns_amount: float = 0.15
    #: never show the same source twice in a row when there is a choice
    avoid_repeats: bool = True


def detect_beats(song_path: str | Path, rate: Fraction,
                 limit: Optional[RationalTime] = None) -> List[RationalTime]:
    """Beat times from the song, or [] when analysis is unavailable.

    librosa is optional on purpose: the engine must still produce a sensible
    edit on a machine that only has ffmpeg, so callers fall back to fixed
    intervals rather than failing.
    """
    try:
        import librosa
    except ImportError:
        return []
    try:
        samples, sample_rate = librosa.load(str(song_path))
        _tempo, beat_frames = librosa.beat.beat_track(y=samples, sr=sample_rate)
        times = librosa.frames_to_time(beat_frames, sr=sample_rate)
    except Exception:
        return []
    beats = [RationalTime.from_seconds(float(t), rate).aligned(rate, NEAREST) for t in times]
    if limit is not None:
        beats = [b for b in beats if b < limit]
    return beats


def plan_cuts(beats: Seq[RationalTime], total: RationalTime, rate: Fraction,
              style: Optional[BeatEditStyle] = None,
              rng: Optional[random.Random] = None) -> List[RationalTime]:
    """Choose cut points: every Nth beat, with a floor on shot length.

    Returns the boundaries including 0 and `total`, so shot i is
    [cuts[i], cuts[i+1]).
    """
    style = style or BeatEditStyle()
    rng = rng or random
    minimum = RationalTime.from_seconds(style.min_shot_seconds, rate)
    cuts: List[RationalTime] = [RationalTime.zero(rate)]

    if beats:
        index = 0
        while index < len(beats):
            index += rng.randint(style.min_beats_per_cut, style.max_beats_per_cut)
            # honour the minimum shot length by walking on to a later beat
            while index < len(beats) and beats[index] - cuts[-1] < minimum:
                index += 1
            if index >= len(beats) or beats[index] >= total:
                break
            cuts.append(beats[index])
    else:
        step = RationalTime.from_seconds(style.fallback_shot_seconds, rate)
        position = cuts[0] + step
        while position < total:
            cuts.append(position)
            position = position + step

    # A final shot shorter than the floor is merged into the one before it,
    # otherwise every song ends on a stutter.
    if len(cuts) > 1 and total - cuts[-1] < minimum:
        cuts.pop()
    cuts.append(total)
    return cuts


def _pick_effect(index: int, duration: RationalTime, style: BeatEditStyle,
                 is_still: bool, rng: random.Random) -> Effect:
    """Choose a per-shot move, so the edit is not the same punch every time."""
    if is_still:
        motion = rng.choice(["zoom_in", "zoom_out", "pan_lr", "pan_rl", "diag"])
        return Effect(new_id("fx"), "ken_burns",
                      {"motion": motion, "amount": style.ken_burns_amount})
    if duration.to_float_seconds() > style.long_shot_seconds:
        kind = rng.choices(["ken_burns", "punch", "plain"], weights=[6, 2, 2])[0]
    elif index > 0 and rng.random() < style.flash_probability:
        kind = "flash"
    else:
        kind = rng.choices(["punch", "plain", "ken_burns"], weights=[5, 3, 2])[0]

    if kind == "punch":
        return Effect(new_id("fx"), "punch", {"amount": style.punch_amount})
    if kind == "flash":
        return Effect(new_id("fx"), "flash", {"duration": 0.1})
    if kind == "ken_burns":
        return Effect(new_id("fx"), "ken_burns",
                      {"motion": "zoom_in", "amount": style.ken_burns_amount * 0.7})
    return Effect(new_id("fx"), "zoom", {"factor": 1.0})    # a clean, still cut


def _source_window(ref: MediaRef, duration: RationalTime, rate: Fraction,
                   rng: random.Random) -> RationalTime:
    """Pick a random in point that leaves room for the whole shot."""
    info = ref.require_info()
    if info.duration_seconds is None:
        return RationalTime.zero(rate)
    available = info.duration(rate) - duration
    if available.value <= 0:
        return RationalTime.zero(rate)
    frames = available.to_frames(rate, "floor")
    return RationalTime.from_frames(rng.randint(0, max(0, frames)), rate)


def build_music_video(project: Project, song_path: str | Path,
                      visual_paths: Iterable[str | Path],
                      sequence: Optional[Sequence] = None,
                      stack: Optional[CommandStack] = None,
                      style: Optional[BeatEditStyle] = None,
                      intro_path: Optional[str | Path] = None,
                      seed: Optional[int] = None,
                      name: str = "Music video") -> Tuple[Sequence, CommandStack]:
    """Build a beat-cut timeline: song on A1, shots on V1, optional intro.

    Returns the sequence and the command stack that built it, so the caller
    can inspect the history, undo parts of the edit, or keep editing.
    """
    style = style or BeatEditStyle()
    rng = random.Random(seed)
    sequence = sequence or project.add_sequence(
        Sequence.create(name=name, rate=Fraction(30), width=1920, height=1080))
    stack = stack or CommandStack(project)
    rate = sequence.rate

    song = project.import_media(song_path)
    song_info = song.require_info()
    if song_info.duration_seconds is None:
        raise ValueError(f"{song_path} has no duration; is it really an audio file?")

    visuals = [project.import_media(path) for path in visual_paths]
    if not visuals:
        raise ValueError("build_music_video needs at least one visual source")

    video_track: Track = sequence.video_tracks[0]
    audio_track: Track = sequence.audio_tracks[0]

    intro_length = RationalTime.zero(rate)
    intro_ref: Optional[MediaRef] = None
    if intro_path is not None:
        intro_ref = project.import_media(intro_path)
        intro_length = intro_ref.require_info().duration(rate).aligned(rate, "floor")

    song_length = song_info.duration(rate).aligned(rate, "floor")
    cuts = plan_cuts(detect_beats(song_path, rate, song_length), song_length, rate, style, rng)

    with stack.transaction(f"Build {name}"):
        if intro_ref is not None:
            stack.run(ops.AddClip(video_track.track_id,
                                  make_clip(intro_ref, sequence, sequence.zero(),
                                            duration=intro_length, name="Intro")), sequence)

        stack.run(ops.AddClip(audio_track.track_id,
                              make_clip(song, sequence, intro_length,
                                        duration=song_length, name=song.name)), sequence)

        previous = -1
        for index in range(len(cuts) - 1):
            start = cuts[index] + intro_length
            duration = cuts[index + 1] - cuts[index]
            if duration.value <= 0:
                continue

            if len(visuals) == 1 or not style.avoid_repeats:
                choice = rng.randrange(len(visuals))
            else:
                choice = rng.choice([i for i in range(len(visuals)) if i != previous])
            previous = choice
            ref = visuals[choice]
            info = ref.require_info()

            source_in = _source_window(ref, duration, rate, rng)
            clip = make_clip(ref, sequence, start, source_in=source_in,
                             duration=duration, name=f"{ref.name} #{index + 1}")
            clip.effects = [_pick_effect(index, duration, style, info.is_still, rng)]
            stack.run(ops.AddClip(video_track.track_id, clip), sequence)

    return sequence, stack


def collect_visuals(*directories: str | Path, kinds: str = "all",
                    report: Optional[List[str]] = None) -> List[Path]:
    """Usable visual sources in the given directories, sorted.

    `kinds` is "video", "image" or "all". It matters more than it looks:
    downloaders drop a .jpg/.webp thumbnail next to every video they fetch, so
    an unfiltered sweep of a clips folder quietly cuts 27 kB poster frames into
    the middle of a montage as if they were shots.

    Anything skipped for an unreadable format is appended to `report` so the
    caller can say so out loud.
    """
    if kinds == "video":
        wanted = VIDEO_SUFFIXES
    elif kinds == "image":
        wanted = IMAGE_SUFFIXES
    else:
        wanted = IMAGE_SUFFIXES | VIDEO_SUFFIXES

    found: List[Path] = []
    for directory in directories:
        path = Path(directory)
        if not path.exists():
            continue
        for candidate in sorted(path.iterdir()):
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix in wanted:
                found.append(candidate)
            elif suffix in UNREADABLE_SUFFIXES and report is not None:
                report.append(candidate.name)
    return found

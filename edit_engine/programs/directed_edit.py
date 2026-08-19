"""
directed_edit.py -- an edit that responds to the footage and the song.

`beat_edit` cuts on the beat and picks everything else at random: which clip,
which moment inside it, which move. It is accurate and blind. This program
keeps the accuracy and replaces the blindness:

    look    measure every source (motion, exposure, internal cuts) and
            describe it (what it is, what it wants) -- see analysis/
    listen  beats say where a cut may land; the energy envelope says what
            kind of cut it should be
    direct  one editorial decision for the whole piece: shot length against
            the beat, which moves suit this material, whether to pull the
            busiest or steadiest part of each clip, how to order against
            energy
    cut     execute that frame-accurately through the same command stack,
            recording the reasoning for every shot

The direction can come from Claude (it sees frames of each source and the
song's tempo) or from measurements alone. Either way the *placement* is done
here, deterministically, from a seed -- the model never positions a frame.

The practical difference from beat_edit: shots are chosen to match the music's
energy at that moment, in-points land on the interesting part of a clip
instead of a random one, and quiet passages get long holds with gentle pushes
while drops get short shots with hard accents.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import ops
from ..analysis import (AnalysisCache, ClipAnalysis, MusicAnalysis, analyse_clip,
                        analyse_music, default_backends)
from ..analysis.vision import EditDirection, SourceDescription
from ..commands import CommandStack
from ..media import MediaRef
from ..model import Effect, Project, Sequence, Track, make_clip, new_id
from ..timebase import NEAREST, RationalTime
from .beat_edit import BeatEditStyle, plan_cuts

#: moves that suit a loud moment vs a quiet one
LOUD_MOVES = ["punch", "flash"]
QUIET_MOVES = ["push", "plain"]


@dataclass
class SourcePlan:
    """Everything known about one source, ready for shot selection."""

    ref: MediaRef
    analysis: Optional[ClipAnalysis]
    description: SourceDescription
    #: 0..1 within this set of sources -- how busy this one is relative to the rest
    motion_rank: float = 0.5
    used_windows: List[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.used_windows is None:
            self.used_windows = []

    @property
    def is_still(self) -> bool:
        info = self.ref.info
        return bool(info and info.is_still)


def _rank_by_motion(plans: List[SourcePlan]) -> None:
    """Give each source a 0..1 position in this set's motion range.

    Relative, not absolute: a montage of calm landscapes still has a busiest
    and a stillest clip, and the edit should use that contrast.
    """
    movers = [(p, p.analysis.mean_motion if p.analysis else 0.0) for p in plans]
    values = sorted(v for _, v in movers)
    if not values or values[-1] == values[0]:
        for plan, _ in movers:
            plan.motion_rank = 0.5
        return
    low, high = values[0], values[-1]
    for plan, value in movers:
        plan.motion_rank = (value - low) / (high - low)


def _choose_source(plans: List[SourcePlan], energy: float, ordering: str,
                   previous: Optional[SourcePlan],
                   rng: random.Random) -> Tuple[SourcePlan, str]:
    """Pick which clip this shot comes from, and say why."""
    candidates = [p for p in plans if p is not previous] or list(plans)

    if ordering == "shuffle":
        choice = rng.choice(candidates)
        return choice, "random pick (shuffle ordering)"

    if ordering == "grouped" and previous is not None:
        same = [p for p in candidates
                if p.description.scene_type == previous.description.scene_type]
        # stay with the subject unless the music has clearly moved on
        if same and energy < 0.7:
            choice = rng.choice(same)
            return choice, (f"stayed on {choice.description.scene_type} to keep the "
                            f"subject together")

    # Energy matching: a loud moment wants the busier footage, a quiet one the
    # stiller. Weight by closeness rather than taking the single best match --
    # always taking the best makes a flat-envelope song repetitive.
    #
    # This must be a weighting, not a "pick randomly from the top k": with only
    # three sources a top-3 pool is every candidate, and the matching silently
    # degrades to the random selection it was meant to replace.
    weights = [1.0 / (abs(p.motion_rank - energy) + 0.12) ** 2 for p in candidates]
    choice = rng.choices(candidates, weights=weights)[0]
    return choice, (f"music energy {energy:.2f}, picked footage with motion rank "
                    f"{choice.motion_rank:.2f}")


def _choose_move(direction: EditDirection, energy: float, seconds: float,
                 is_still: bool, style: BeatEditStyle,
                 rng: random.Random) -> Tuple[Effect, str]:
    """Pick the camera move for one shot, and say why."""
    if is_still:
        motion = rng.choice(["zoom_in", "zoom_out", "pan_lr", "pan_rl", "diag"])
        return (Effect(new_id("fx"), "ken_burns",
                       {"motion": motion, "amount": style.ken_burns_amount}),
                f"still photo -> Ken Burns ({motion})")

    allowed = [m for m in direction.preferred_moves if m != "ken_burns"] or ["plain"]
    if energy >= 0.7:
        pool = [m for m in allowed if m in LOUD_MOVES] or allowed
        why = f"loud moment (energy {energy:.2f})"
    elif energy <= 0.35:
        pool = [m for m in allowed if m in QUIET_MOVES] or allowed
        why = f"quiet moment (energy {energy:.2f})"
    else:
        pool = allowed
        why = f"mid energy ({energy:.2f})"

    kind = rng.choice(pool)
    if kind == "punch":
        return (Effect(new_id("fx"), "punch", {"amount": style.punch_amount}),
                f"{why} -> punch")
    if kind == "flash":
        return (Effect(new_id("fx"), "flash", {"duration": 0.1}), f"{why} -> flash")
    if kind == "push":
        pushed = rng.choice(["in", "in", "out"])
        return (Effect(new_id("fx"), "push",
                       {"amount": style.push_amount, "direction": pushed}),
                f"{why} -> slow push {pushed}")
    return (Effect(new_id("fx"), "zoom", {"factor": 1.0}), f"{why} -> clean cut")


def _biased_style(style: BeatEditStyle, direction: EditDirection) -> BeatEditStyle:
    """Apply the director's shot-length bias to the cut planner's parameters."""
    bias = max(0.4, min(3.0, direction.shot_length_bias))
    return BeatEditStyle(
        min_beats_per_cut=max(1, round(style.min_beats_per_cut * bias)),
        max_beats_per_cut=max(2, round(style.max_beats_per_cut * bias)),
        min_shot_seconds=style.min_shot_seconds * bias,
        fallback_shot_seconds=style.fallback_shot_seconds * bias,
        flash_probability=style.flash_probability,
        long_shot_seconds=style.long_shot_seconds,
        punch_amount=style.punch_amount,
        ken_burns_amount=style.ken_burns_amount,
        push_amount=style.push_amount,
        avoid_repeats=style.avoid_repeats,
    )


def build_directed_video(project: Project, song_path: str | Path,
                         visual_paths: Iterable[str | Path],
                         sequence: Optional[Sequence] = None,
                         stack: Optional[CommandStack] = None,
                         style: Optional[BeatEditStyle] = None,
                         intro_path: Optional[str | Path] = None,
                         seed: Optional[int] = None,
                         use_vision: bool = True,
                         cache_path: Optional[str | Path] = None,
                         vision=None, director=None, on_progress=None,
                         name: str = "Music video") -> Tuple[Sequence, CommandStack]:
    """Build a timeline directed by what the footage and the song actually are."""
    style = style or BeatEditStyle()
    rng = random.Random(seed)
    sequence = sequence or project.add_sequence(
        Sequence.create(name=name, rate=Fraction(30), width=1920, height=1080))
    stack = stack or CommandStack(project)
    rate = sequence.rate

    cache = AnalysisCache(cache_path)
    backend_note = "custom backends"
    if vision is None or director is None:
        default_vision, default_director, backend_note = default_backends(use_vision, cache)
        vision = vision or default_vision
        director = director or default_director

    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    report(backend_note)

    # -- look and listen --------------------------------------------------
    song = project.import_media(song_path)
    song_info = song.require_info()
    if song_info.duration_seconds is None:
        raise ValueError(f"{song_path} has no duration; is it really an audio file?")
    music = analyse_music(song_path, cache)

    visual_paths = list(visual_paths)
    plans: List[SourcePlan] = []
    for index, path in enumerate(visual_paths, start=1):
        ref = project.import_media(path)
        info = ref.require_info()
        # Scanning a 4K clip takes real seconds; without this the terminal
        # looks hung for minutes on a folder of large sources.
        report(f"analysing {index}/{len(visual_paths)}: {ref.name}")
        analysis = None if info.is_still else analyse_clip(ref.path, cache)
        plans.append(SourcePlan(ref=ref, analysis=analysis,
                                description=vision.describe(ref.path, analysis)))
    if not plans:
        raise ValueError("build_directed_video needs at least one visual source")
    _rank_by_motion(plans)

    # -- direct -----------------------------------------------------------
    song_length = song_info.duration(rate).aligned(rate, "floor")
    music_summary = {
        "tempo_bpm": music.tempo_bpm,
        "beat_count": len(music.beats),
        "song_seconds": float(song_length.to_seconds()),
    }
    report("deciding the editorial approach")
    direction = director.direct([p.description for p in plans], music_summary)
    cut_style = _biased_style(style, direction)

    beats = [RationalTime.from_seconds(b, rate).aligned(rate, NEAREST)
             for b in music.beats if b < float(song_length.to_seconds())]
    cuts = plan_cuts(beats, song_length, rate, cut_style, rng)

    sequence.metadata["analysis"] = {
        "program": "directed_edit",
        "seed": seed,
        "cut_mode": "beats" if beats else "fixed interval (no beat data)",
        "beat_count": len(beats),
        "beats": [round(b.to_float_seconds(), 4) for b in beats],
        "tempo_bpm": music.tempo_bpm,
        "song": song.name,
        "song_seconds": round(float(song_length.to_seconds()), 3),
        "visual_sources": [p.ref.name for p in plans],
        "backend_note": backend_note,
        "beats_available": music.beats_available,
        "direction": direction.to_dict(),
        "sources": [dict(p.description.to_dict(),
                         motion_rank=round(p.motion_rank, 3),
                         mean_motion=round(p.analysis.mean_motion, 5) if p.analysis else None)
                    for p in plans],
        "style": {
            "min_beats_per_cut": cut_style.min_beats_per_cut,
            "max_beats_per_cut": cut_style.max_beats_per_cut,
            "min_shot_seconds": round(cut_style.min_shot_seconds, 3),
            "long_shot_seconds": cut_style.long_shot_seconds,
            "flash_probability": cut_style.flash_probability,
            "shot_length_bias": direction.shot_length_bias,
        },
    }

    # -- cut --------------------------------------------------------------
    video_track: Track = sequence.video_tracks[0]
    audio_track: Track = sequence.audio_tracks[0]

    intro_length = RationalTime.zero(rate)
    intro_ref: Optional[MediaRef] = None
    if intro_path is not None:
        intro_ref = project.import_media(intro_path)
        intro_length = intro_ref.require_info().duration(rate).aligned(rate, "floor")

    beat_set = {round(b.to_float_seconds(), 3) for b in beats}

    with stack.transaction(f"Build {name}"):
        if intro_ref is not None:
            stack.run(ops.AddClip(video_track.track_id,
                                  make_clip(intro_ref, sequence, sequence.zero(),
                                            duration=intro_length, name="Intro")), sequence)
        stack.run(ops.AddClip(audio_track.track_id,
                              make_clip(song, sequence, intro_length,
                                        duration=song_length, name=song.name)), sequence)

        previous: Optional[SourcePlan] = None
        for index in range(len(cuts) - 1):
            start = cuts[index] + intro_length
            duration = cuts[index + 1] - cuts[index]
            if duration.value <= 0:
                continue
            seconds = duration.to_float_seconds()
            energy = music.mean_energy_over(cuts[index].to_float_seconds(), seconds)

            plan, source_reason = _choose_source(plans, energy, direction.ordering,
                                                 previous, rng)
            previous = plan

            if plan.analysis is not None:
                in_point, score = plan.analysis.best_window(
                    seconds, prefer=direction.prefer_windows,
                    avoid=plan.used_windows)
                plan.used_windows.append(in_point)
                in_reason = (f"best {direction.prefer_windows} window at "
                             f"{in_point:.1f}s (score {score:.2f})")
            else:
                in_point, in_reason = 0.0, "still image, no in-point"

            source_in = RationalTime.from_seconds(in_point, rate).aligned(rate, "floor")
            clip = make_clip(plan.ref, sequence, start, source_in=source_in,
                             duration=duration, name=f"{plan.ref.name} #{index + 1}")
            effect, effect_reason = _choose_move(direction, energy, seconds,
                                                 plan.is_still, style, rng)
            clip.effects = [effect]

            cut_at = round(cuts[index].to_float_seconds(), 3)
            clip.metadata["decision"] = {
                "shot": index + 1,
                "cut_on_beat": index == 0 or cut_at in beat_set,
                "cut_at_seconds": cut_at,
                "duration_seconds": round(seconds, 3),
                "music_energy": round(energy, 3),
                "source_reason": source_reason,
                "source_in_reason": in_reason,
                "effect_reason": effect_reason,
                "content": plan.description.subject,
            }
            stack.run(ops.AddClip(video_track.track_id, clip), sequence)

    cache.save()
    return sequence, stack

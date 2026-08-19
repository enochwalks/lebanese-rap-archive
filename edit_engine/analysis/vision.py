"""
vision.py -- understanding what the footage *is*, and deciding how to cut it.

DIVISION OF LABOUR
------------------
Claude directs. The engine executes.

Claude is good at judgement -- "this is a slow moon shot over water, it wants
long holds and a gentle push, not jump cuts" -- and bad at frame-accurate
placement. The engine is the opposite. So the model never places a frame: it
returns a *description* of each source and a *direction* for the edit, both as
small structured objects, and the deterministic planner turns those into
frame-exact cuts. That keeps the edit reproducible, undoable and inspectable,
and it means a bad API day degrades the taste of the edit, never its
correctness.

COST
----
One call per *source file* (not per shot), cached on disk forever, plus one
director call per video. A montage with ten clips costs eleven calls the first
time and one thereafter. Descriptions survive in the cache keyed by file
identity, so re-rolling a seed or re-rendering costs nothing.

FALLBACK
--------
No API key, no SDK, or a failed call falls back to `MeasurementVision` /
`HeuristicDirector`, which work from the ffmpeg measurements alone. The edit
still gets made; it just loses the content judgement. Which backend ran is
recorded on every description, so the report never implies the model saw
something it did not.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from .cache import AnalysisCache
from .media import ClipAnalysis, sample_frames

MODEL = os.environ.get("EDIT_ENGINE_MODEL", "claude-opus-5")
VISION_VERSION = 1

SCENE_TYPES = ["landscape", "action", "portrait", "crowd", "urban", "interior",
               "abstract", "text_or_graphic", "other"]
MOODS = ["calm", "melancholic", "dreamy", "triumphant", "tense", "aggressive",
         "playful", "neutral"]
PACES = ["static", "slow", "moderate", "fast"]


@dataclass
class SourceDescription:
    """What one source file contains, and what it wants editorially."""

    path: str
    subject: str = ""
    scene_type: str = "other"
    mood: str = "neutral"
    pace: str = "moderate"
    palette: str = ""
    tags: List[str] = field(default_factory=list)
    edit_notes: str = ""
    #: which backend produced this -- "claude" or "measurement"
    backend: str = "measurement"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceDescription":
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known.setdefault("path", data.get("path", ""))
        return cls(**{k: v for k, v in known.items() if v is not None})


@dataclass
class EditDirection:
    """How this particular edit should be cut."""

    style_name: str = "default"
    #: multiplies the beats-per-cut range; >1 = longer shots
    shot_length_bias: float = 1.0
    #: effect kinds the planner may choose from, in preference order
    preferred_moves: List[str] = field(default_factory=lambda: ["punch", "plain", "push"])
    #: how to choose in-points: "action" or "calm"
    prefer_windows: str = "action"
    #: "energy" matches shot motion to music energy; "grouped" keeps similar
    #: subjects together; "shuffle" is the old random behaviour
    ordering: str = "energy"
    rationale: str = ""
    backend: str = "heuristic"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EditDirection":
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        return cls(**{k: v for k, v in known.items() if v is not None})


class VisionBackend(Protocol):
    def describe(self, path: str, analysis: Optional[ClipAnalysis]) -> SourceDescription: ...


class DirectorBackend(Protocol):
    def direct(self, descriptions: List[SourceDescription],
               music: Dict[str, Any]) -> EditDirection: ...


# ---------------------------------------------------------------------------
# no-API backends: work from the ffmpeg measurements alone
# ---------------------------------------------------------------------------

class MeasurementVision:
    """Describe a source from its motion/luma/saturation statistics.

    Cannot tell a moon from a shop -- it only knows fast from slow and dark
    from bright -- but it needs no network, no key and no money.
    """

    def describe(self, path: str, analysis: Optional[ClipAnalysis]) -> SourceDescription:
        name = Path(path).stem
        if analysis is None:
            return SourceDescription(path=path, subject=name)
        motion, luma, sat = analysis.mean_motion, analysis.mean_luma, analysis.mean_saturation
        pace = ("fast" if motion > 0.045 else "moderate" if motion > 0.018
                else "slow" if motion > 0.004 else "static")
        mood = ("aggressive" if motion > 0.045
                else "melancholic" if luma < 0.3
                else "dreamy" if sat < 0.12 else "neutral")
        return SourceDescription(
            path=path, subject=name,
            scene_type="action" if motion > 0.04 else "landscape" if motion < 0.01 else "other",
            mood=mood, pace=pace,
            palette=("dark" if luma < 0.3 else "bright" if luma > 0.65 else "mid") +
                    (", desaturated" if sat < 0.12 else ""),
            tags=[], edit_notes="described from motion/brightness measurements only",
            backend="measurement")


class HeuristicDirector:
    """Choose a style from tempo and how busy the footage is."""

    def direct(self, descriptions: List[SourceDescription],
               music: Dict[str, Any]) -> EditDirection:
        tempo = music.get("tempo_bpm") or 0
        fast_sources = sum(1 for d in descriptions if d.pace in ("fast", "moderate"))
        busy = descriptions and fast_sources / len(descriptions) > 0.5

        if busy and tempo >= 120:
            return EditDirection(
                style_name="hard-cut action", shot_length_bias=0.8,
                preferred_moves=["punch", "flash", "plain"], prefer_windows="action",
                ordering="energy",
                rationale=f"busy footage at {tempo:.0f} bpm: short shots, hard accents",
                backend="heuristic")
        if not busy:
            return EditDirection(
                style_name="slow cinematic", shot_length_bias=1.6,
                preferred_moves=["push", "plain"], prefer_windows="calm",
                ordering="energy",
                rationale="mostly steady footage: long holds and gentle pushes",
                backend="heuristic")
        return EditDirection(
            style_name="balanced", shot_length_bias=1.0,
            preferred_moves=["punch", "plain", "push"], prefer_windows="action",
            ordering="energy", rationale="mixed footage", backend="heuristic")


# ---------------------------------------------------------------------------
# Claude backends
# ---------------------------------------------------------------------------

_DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string",
                    "description": "What is actually on screen, in a few words"},
        "scene_type": {"type": "string", "enum": SCENE_TYPES},
        "mood": {"type": "string", "enum": MOODS},
        "pace": {"type": "string", "enum": PACES},
        "palette": {"type": "string", "description": "Dominant colours, few words"},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "3-6 short keywords"},
        "edit_notes": {"type": "string",
                       "description": "One sentence: how this footage wants to be cut"},
    },
    "required": ["subject", "scene_type", "mood", "pace", "palette", "tags", "edit_notes"],
    "additionalProperties": False,
}

_DIRECT_SCHEMA = {
    "type": "object",
    "properties": {
        "style_name": {"type": "string", "description": "Short name for the approach"},
        "shot_length_bias": {"type": "number",
                             "description": "0.5 = much shorter shots, 2.0 = much longer"},
        "preferred_moves": {"type": "array", "items": {
            "type": "string", "enum": ["punch", "flash", "push", "plain", "ken_burns"]}},
        "prefer_windows": {"type": "string", "enum": ["action", "calm"]},
        "ordering": {"type": "string", "enum": ["energy", "grouped", "shuffle"]},
        "rationale": {"type": "string",
                      "description": "Two sentences on why this suits this song and footage"},
    },
    "required": ["style_name", "shot_length_bias", "preferred_moves",
                 "prefer_windows", "ordering", "rationale"],
    "additionalProperties": False,
}


def _friendly(message: str) -> str:
    """Turn an API error into something worth reading in a terminal."""
    lowered = message.lower()
    if "credit balance" in lowered:
        return ("your Anthropic account has no API credits -- add them at "
                "console.anthropic.com under Plans & Billing (a Claude.ai "
                "subscription is separate and does not cover API use)")
    if "authentication_error" in lowered or "invalid x-api-key" in lowered:
        return ("the API key was rejected -- check ANTHROPIC_API_KEY is set to a "
                "complete, current key")
    if "permission_error" in lowered:
        return "this API key is not permitted to use that model"
    if "rate_limit" in lowered:
        return "rate limited by the API; try again shortly"
    return message


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "the anthropic SDK is not installed; run `pip install anthropic` "
            "or use the measurement backend") from exc
    return anthropic.Anthropic()


class ClaudeVision:
    """Show Claude a few frames of each source and ask what it is."""

    #: errors that are about the account, not about this clip. Retrying them
    #: once per source just burns time -- eleven identical failures tell you
    #: nothing the first one did not.
    FATAL_MARKERS = ("credit balance", "authentication_error", "invalid x-api-key",
                     "permission_error", "api key")

    def __init__(self, model: str = MODEL, frames: int = 3,
                 cache: Optional[AnalysisCache] = None):
        self.model = model
        self.frames = frames
        self.cache = cache
        self._client = None
        self._giving_up: Optional[str] = None

    def describe(self, path: str, analysis: Optional[ClipAnalysis]) -> SourceDescription:
        if self.cache is not None:
            key = AnalysisCache.key(path, "vision", VISION_VERSION,
                                    model=self.model, frames=self.frames)
            cached = self.cache.get(key)
            if cached is not None:
                return SourceDescription.from_dict(cached)

        description = self._describe_uncached(path, analysis)

        if self.cache is not None and description.backend == "claude":
            self.cache.put(
                AnalysisCache.key(path, "vision", VISION_VERSION,
                                  model=self.model, frames=self.frames),
                description.to_dict())
        return description

    def _describe_uncached(self, path: str,
                           analysis: Optional[ClipAnalysis]) -> SourceDescription:
        if self._giving_up is not None:
            fallback = MeasurementVision().describe(path, analysis)
            fallback.edit_notes = f"vision unavailable ({self._giving_up}); {fallback.edit_notes}"
            return fallback

        images = sample_frames(path, count=self.frames)
        if not images:
            return MeasurementVision().describe(path, analysis)

        measured = ""
        if analysis is not None:
            measured = (f"\nMeasured from the file: mean motion "
                        f"{analysis.mean_motion:.4f} (0=locked off, 0.08=fast action), "
                        f"mean brightness {analysis.mean_luma:.2f}, "
                        f"{len(analysis.scene_cuts)} internal cuts, "
                        f"{analysis.duration:.0f}s long.")

        content: List[Dict[str, Any]] = [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image).decode("ascii")}}
            for image in images
        ]
        content.append({"type": "text", "text":
            f"These are {len(images)} frames sampled evenly from one source clip "
            f"named '{Path(path).name}', which will be used as footage in a "
            f"music video.{measured}\n\n"
            f"Describe what this footage is and how it wants to be cut. Judge the "
            f"content, not the file name."})

        try:
            if self._client is None:
                self._client = _client()
            response = self._client.messages.create(
                model=self.model,
                max_tokens=2000,
                # perception, not deliberation: low effort is the right spend here
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": _DESCRIBE_SCHEMA}},
                messages=[{"role": "user", "content": content}],
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("model declined to describe this footage")
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
        except Exception as error:                       # noqa: BLE001
            message = str(error)
            if any(marker in message.lower() for marker in self.FATAL_MARKERS):
                self._giving_up = _friendly(message)
            fallback = MeasurementVision().describe(path, analysis)
            reason = self._giving_up or message
            fallback.edit_notes = f"vision unavailable ({reason}); {fallback.edit_notes}"
            return fallback

        data["path"] = path
        data["backend"] = "claude"
        return SourceDescription.from_dict(data)


class ClaudeDirector:
    """Give Claude the whole picture -- every source plus the music -- and ask
    for the editorial approach."""

    def __init__(self, model: str = MODEL):
        self.model = model
        self._client = None

    def direct(self, descriptions: List[SourceDescription],
               music: Dict[str, Any]) -> EditDirection:
        listing = "\n".join(
            f"- {Path(d.path).name}: {d.subject} "
            f"[{d.scene_type}, {d.mood}, {d.pace} pace, {d.palette}] {d.edit_notes}"
            for d in descriptions)
        song = (f"{music.get('tempo_bpm') or 'unknown'} bpm, "
                f"{music.get('song_seconds', 0):.0f}s long, "
                f"{music.get('beat_count', 0)} beats detected")

        prompt = (
            f"You are directing a music video edit.\n\n"
            f"SONG: {song}\n\nFOOTAGE:\n{listing}\n\n"
            f"An engine will cut this frame-accurately on the beat. Your job is "
            f"the editorial approach, not the individual cuts. Decide how long "
            f"shots should run relative to the beat, which camera moves suit "
            f"this material, whether to pull the busiest or the steadiest part "
            f"of each clip, and how to order shots against the music's energy. "
            f"Match the footage: still landscapes and fight scenes do not want "
            f"the same treatment.")

        try:
            if self._client is None:
                self._client = _client()
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4000,
                # this is the judgement call, so it gets a real thinking budget
                output_config={"effort": "medium",
                               "format": {"type": "json_schema", "schema": _DIRECT_SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("model declined to direct this edit")
            data = json.loads(next(b.text for b in response.content if b.type == "text"))
        except Exception as error:                       # noqa: BLE001
            fallback = HeuristicDirector().direct(descriptions, music)
            fallback.rationale = (f"director unavailable ({_friendly(str(error))}); "
                                  f"{fallback.rationale}")
            return fallback

        data["backend"] = "claude"
        direction = EditDirection.from_dict(data)
        # Trust the taste, bound the arithmetic: a bias of 8 would produce one
        # shot for the whole song.
        direction.shot_length_bias = max(0.4, min(3.0, float(direction.shot_length_bias)))
        return direction


def vision_available() -> tuple[bool, str]:
    """Can we actually call the API? Returns (available, reason).

    Checked up front rather than discovered per clip, so the run can say what
    it is really going to do instead of announcing Claude and then silently
    falling back eleven times.
    """
    try:
        import anthropic
    except ImportError:
        return False, "the anthropic SDK is not installed (py -m pip install anthropic)"
    try:
        anthropic.Anthropic()
    except Exception as error:                           # noqa: BLE001
        return False, f"no API credentials ({error})"
    return True, "ready"


def default_backends(use_vision: bool, cache: Optional[AnalysisCache] = None):
    """(vision, director, note) for a run. Falls back cleanly when unavailable.

    `note` states what will actually happen, so callers can print the truth.
    """
    if not use_vision:
        return (MeasurementVision(), HeuristicDirector(),
                "vision disabled; directing from measurements")
    available, reason = vision_available()
    if not available:
        return (MeasurementVision(), HeuristicDirector(),
                f"vision unavailable -- {reason}; directing from measurements instead")
    return ClaudeVision(cache=cache), ClaudeDirector(), "vision enabled (Claude)"

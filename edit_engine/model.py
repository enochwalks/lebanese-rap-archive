"""
model.py -- the non-destructive timeline data model.

SHAPE
-----
    Project
      +- MediaRegistry          (source files, never modified)
      +- Sequence(s)
           +- video tracks  V1..Vn   (V1 = bottom of the composite)
           +- audio tracks  A1..An
                +- Clip: a *window onto* a media file, placed at a time

A Clip owns no pixels. It owns: which media, which part of it (source_range),
where it sits (start), how fast it plays (speed), and how it is treated
(enabled, gain, effects). Editing changes those numbers and nothing else --
that is what "non-destructive" actually means in an engine, and it is why
undo can be exact.

TWO INVARIANTS THE MODEL ENFORCES ITSELF
----------------------------------------
* Clips on a track are sorted by start time and never overlap. Gaps are not
  objects; they are the space between clips, computed on demand. Storing gap
  objects means two representations of the same fact, and they drift.
* A clip's source_range must lie inside the media's available range. Trims
  clamp at the media limit rather than silently producing a range that only
  fails at render time, three hours into a batch.

Commands (see commands.py) are the only thing that should mutate a Sequence,
because they are what makes an edit undoable.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence as SequenceType, Tuple

from .media import MediaRef, MediaRegistry
from .timebase import (NEAREST, RationalTime, TimeRange, normalize_rate,
                       to_fraction)

SCHEMA_VERSION = 1

VIDEO = "V"
AUDIO = "A"


class ModelError(ValueError):
    """Raised when an operation would break a timeline invariant."""


class OverlapError(ModelError):
    pass


def new_id(prefix: str) -> str:
    """Short unique id for a model object. Used across the package, so it is
    part of the public surface rather than a private helper."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Marker:
    """A named point (or range) on a sequence or clip."""

    marker_id: str
    time: RationalTime
    name: str = ""
    duration: Optional[RationalTime] = None
    color: str = "yellow"
    note: str = ""

    @property
    def range(self) -> TimeRange:
        zero = RationalTime.zero(self.time.rate)
        return TimeRange(self.time, self.duration or zero)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "marker_id": self.marker_id, "name": self.name,
            "time": _time_to_dict(self.time),
            "duration": _time_to_dict(self.duration) if self.duration else None,
            "color": self.color, "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Marker":
        return cls(marker_id=data["marker_id"], time=_time_from_dict(data["time"]),
                   name=data.get("name", ""),
                   duration=_time_from_dict(data["duration"]) if data.get("duration") else None,
                   color=data.get("color", "yellow"), note=data.get("note", ""))


@dataclass
class Effect:
    """An effect instance on a clip. Phase 1 carries and serializes them so the
    model does not need a breaking change later; the renderer applies the ones
    it understands and reports the rest rather than dropping them silently."""

    effect_id: str
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"effect_id": self.effect_id, "kind": self.kind,
                "params": dict(self.params), "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Effect":
        return cls(effect_id=data["effect_id"], kind=data["kind"],
                   params=dict(data.get("params", {})), enabled=data.get("enabled", True))


@dataclass
class Clip:
    """One window onto one media file, placed on one track."""

    clip_id: str
    media_id: str
    source_range: TimeRange           # source-local time
    start: RationalTime               # timeline position of the clip's head
    name: str = ""
    enabled: bool = True
    speed: Fraction = Fraction(1)     # 1 = realtime, 2 = double, -1 = reverse
    link_id: Optional[str] = None     # shared by the A and V halves of a take
    gain_db: float = 0.0
    effects: List[Effect] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.speed = to_fraction(self.speed)
        if self.speed == 0:
            raise ModelError("clip speed cannot be 0 (use a freeze frame instead)")

    # -- geometry --------------------------------------------------------

    @property
    def duration(self) -> RationalTime:
        """Length on the *timeline*, which is source length / |speed|."""
        return self.source_range.duration / abs(self.speed)

    @property
    def range(self) -> TimeRange:
        return TimeRange(self.start, self.duration)

    @property
    def end(self) -> RationalTime:
        return self.start + self.duration

    @property
    def source_in(self) -> RationalTime:
        return self.source_range.start_time

    @property
    def source_out_exclusive(self) -> RationalTime:
        return self.source_range.end_time_exclusive

    @property
    def is_reversed(self) -> bool:
        return self.speed < 0

    def source_time_at(self, timeline_time: RationalTime) -> RationalTime:
        """Map a timeline instant to the source instant shown at it.

        This is `match frame`, and it is also what every trim operation needs
        in order to keep the picture stationary while the edit point moves.
        """
        offset = (timeline_time - self.start) * abs(self.speed)
        if self.is_reversed:
            return self.source_range.end_time_exclusive - offset
        return self.source_range.start_time + offset

    def with_(self, **changes: Any) -> "Clip":
        """Copy with fields replaced (commands prefer copies over mutation)."""
        clone = copy.deepcopy(self)
        for key, value in changes.items():
            setattr(clone, key, value)
        return clone

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clip_id": self.clip_id, "media_id": self.media_id, "name": self.name,
            "source_range": _range_to_dict(self.source_range),
            "start": _time_to_dict(self.start),
            "enabled": self.enabled, "speed": str(self.speed),
            "link_id": self.link_id, "gain_db": self.gain_db,
            "effects": [e.to_dict() for e in self.effects],
            "markers": [m.to_dict() for m in self.markers],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Clip":
        return cls(
            clip_id=data["clip_id"], media_id=data["media_id"],
            source_range=_range_from_dict(data["source_range"]),
            start=_time_from_dict(data["start"]),
            name=data.get("name", ""), enabled=data.get("enabled", True),
            speed=Fraction(data.get("speed", "1")), link_id=data.get("link_id"),
            gain_db=data.get("gain_db", 0.0),
            effects=[Effect.from_dict(e) for e in data.get("effects", [])],
            markers=[Marker.from_dict(m) for m in data.get("markers", [])],
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Track:
    """An ordered, non-overlapping lane of clips."""

    track_id: str
    kind: str = VIDEO                  # VIDEO or AUDIO
    name: str = ""
    clips: List[Clip] = field(default_factory=list)
    enabled: bool = True               # contributes to the render
    locked: bool = False               # refuses edits
    muted: bool = False                # audio
    solo: bool = False
    targeted: bool = True              # receives insert/overwrite edits
    sync_lock: bool = True             # ripples along with edits on other tracks
    volume_db: float = 0.0

    # -- queries ---------------------------------------------------------

    def __iter__(self) -> Iterator[Clip]:
        return iter(self.clips)

    def __len__(self) -> int:
        return len(self.clips)

    @property
    def is_audio(self) -> bool:
        return self.kind == AUDIO

    def duration(self, rate) -> RationalTime:
        if not self.clips:
            return RationalTime.zero(rate)
        return max(clip.end for clip in self.clips)

    def index_of(self, clip_id: str) -> int:
        for i, clip in enumerate(self.clips):
            if clip.clip_id == clip_id:
                return i
        raise ModelError(f"clip {clip_id!r} is not on track {self.name or self.track_id!r}")

    def find(self, clip_id: str) -> Clip:
        return self.clips[self.index_of(clip_id)]

    def has(self, clip_id: str) -> bool:
        return any(c.clip_id == clip_id for c in self.clips)

    def clip_at(self, time: RationalTime) -> Optional[Clip]:
        """The clip under a playhead position (half-open, so an edit point
        belongs to the clip that starts there)."""
        for clip in self.clips:
            if clip.start <= time < clip.end:
                return clip
        return None

    def clips_in_range(self, span: TimeRange) -> List[Clip]:
        return [c for c in self.clips if c.range.overlaps(span)]

    def clips_starting_at_or_after(self, time: RationalTime) -> List[Clip]:
        return [c for c in self.clips if c.start >= time]

    def neighbours(self, clip_id: str) -> Tuple[Optional[Clip], Optional[Clip]]:
        i = self.index_of(clip_id)
        before = self.clips[i - 1] if i > 0 else None
        after = self.clips[i + 1] if i + 1 < len(self.clips) else None
        return before, after

    def edit_points(self, rate) -> List[RationalTime]:
        """Every cut on this track, for `jump to next/previous edit`."""
        points: List[RationalTime] = [RationalTime.zero(rate)]
        for clip in self.clips:
            points.append(clip.start)
            points.append(clip.end)
        return sorted(set(points))

    def gap_at(self, time: RationalTime, rate) -> Optional[TimeRange]:
        """The empty span containing `time`, if `time` is not over a clip."""
        if self.clip_at(time) is not None:
            return None
        start = RationalTime.zero(rate)
        for clip in self.clips:
            if clip.start > time:
                return TimeRange.from_start_end(start, clip.start)
            start = max(start, clip.end)
        return None  # trailing empty space is unbounded, not a gap

    def free_space_at(self, span: TimeRange, ignore: SequenceType[str] = ()) -> bool:
        return not any(c.range.overlaps(span) for c in self.clips
                       if c.clip_id not in ignore)

    # -- mutation (invariant-checked; use commands for undoable edits) ----

    def place(self, clip: Clip) -> None:
        """Insert a clip, refusing to create an overlap."""
        for existing in self.clips:
            if existing.range.overlaps(clip.range):
                raise OverlapError(
                    f"clip {clip.name or clip.clip_id!r} at {clip.range} overlaps "
                    f"{existing.name or existing.clip_id!r} at {existing.range}")
        self.clips.append(clip)
        self.sort()

    def remove(self, clip_id: str) -> Clip:
        clip = self.clips.pop(self.index_of(clip_id))
        return clip

    def sort(self) -> None:
        self.clips.sort(key=lambda c: c.start.to_seconds())

    def snapshot(self) -> List[Clip]:
        return copy.deepcopy(self.clips)

    def restore(self, clips: List[Clip]) -> None:
        self.clips = copy.deepcopy(clips)
        self.sort()

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "track_id": self.track_id, "kind": self.kind, "name": self.name,
            "enabled": self.enabled, "locked": self.locked, "muted": self.muted,
            "solo": self.solo, "targeted": self.targeted,
            "sync_lock": self.sync_lock, "volume_db": self.volume_db,
            "clips": [c.to_dict() for c in self.clips],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Track":
        return cls(
            track_id=data["track_id"], kind=data.get("kind", VIDEO),
            name=data.get("name", ""), enabled=data.get("enabled", True),
            locked=data.get("locked", False), muted=data.get("muted", False),
            solo=data.get("solo", False), targeted=data.get("targeted", True),
            sync_lock=data.get("sync_lock", True), volume_db=data.get("volume_db", 0.0),
            clips=[Clip.from_dict(c) for c in data.get("clips", [])],
        )


@dataclass
class Sequence:
    """A timeline: a raster, a timebase, and stacks of video and audio tracks."""

    sequence_id: str
    name: str = "Sequence"
    rate: Fraction = Fraction(30)      # frames per second, exact
    width: int = 1920
    height: int = 1080
    sample_rate: int = 48000
    channels: int = 2
    video_tracks: List[Track] = field(default_factory=list)
    audio_tracks: List[Track] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    in_point: Optional[RationalTime] = None
    out_point: Optional[RationalTime] = None
    background_color: str = "black"

    def __post_init__(self) -> None:
        self.rate = normalize_rate(self.rate)

    # -- construction helpers -------------------------------------------

    @classmethod
    def create(cls, name: str = "Sequence", rate=Fraction(30), width: int = 1920,
               height: int = 1080, video_tracks: int = 1, audio_tracks: int = 1,
               sample_rate: int = 48000, channels: int = 2) -> "Sequence":
        seq = cls(sequence_id=new_id("seq"), name=name, rate=normalize_rate(rate),
                  width=width, height=height, sample_rate=sample_rate, channels=channels)
        for i in range(video_tracks):
            seq.add_track(VIDEO, f"V{i + 1}")
        for i in range(audio_tracks):
            seq.add_track(AUDIO, f"A{i + 1}")
        return seq

    def add_track(self, kind: str = VIDEO, name: str = "") -> Track:
        stack = self.video_tracks if kind == VIDEO else self.audio_tracks
        track = Track(track_id=new_id("trk"), kind=kind,
                      name=name or f"{kind}{len(stack) + 1}")
        stack.append(track)
        return track

    # -- time helpers ----------------------------------------------------

    def frames(self, count: int) -> RationalTime:
        return RationalTime.from_frames(count, self.rate)

    def seconds(self, value) -> RationalTime:
        return RationalTime.from_seconds(value, self.rate)

    def zero(self) -> RationalTime:
        return RationalTime.zero(self.rate)

    def snap_to_frame(self, time: RationalTime, rounding: str = NEAREST) -> RationalTime:
        return time.rescaled_to(self.rate, rounding)

    # -- track access ----------------------------------------------------

    @property
    def tracks(self) -> List[Track]:
        return list(self.video_tracks) + list(self.audio_tracks)

    def track(self, track_id: str) -> Track:
        for t in self.tracks:
            if t.track_id == track_id:
                return t
        raise ModelError(f"unknown track id {track_id!r}")

    def track_by_name(self, name: str) -> Track:
        for t in self.tracks:
            if t.name == name:
                return t
        raise ModelError(f"no track named {name!r}")

    def targeted_tracks(self, kind: Optional[str] = None) -> List[Track]:
        return [t for t in self.tracks
                if t.targeted and not t.locked and (kind is None or t.kind == kind)]

    def find_clip(self, clip_id: str) -> Tuple[Track, Clip]:
        for track in self.tracks:
            if track.has(clip_id):
                return track, track.find(clip_id)
        raise ModelError(f"clip {clip_id!r} is not in sequence {self.name!r}")

    def linked_clips(self, link_id: Optional[str]) -> List[Tuple[Track, Clip]]:
        if not link_id:
            return []
        found = []
        for track in self.tracks:
            for clip in track.clips:
                if clip.link_id == link_id:
                    found.append((track, clip))
        return found

    # -- extents ---------------------------------------------------------

    @property
    def duration(self) -> RationalTime:
        tracks = self.tracks
        if not tracks:
            return self.zero()
        return max((t.duration(self.rate) for t in tracks), default=self.zero())

    @property
    def range(self) -> TimeRange:
        return TimeRange(self.zero(), self.duration)

    @property
    def work_range(self) -> TimeRange:
        """In/out points if set, otherwise the whole sequence."""
        start = self.in_point or self.zero()
        end = self.out_point or self.duration
        if end <= start:
            return TimeRange(start, self.zero())
        return TimeRange.from_start_end(start, end)

    def edit_points(self, tracks: Optional[SequenceType[Track]] = None) -> List[RationalTime]:
        points = set()
        for track in (tracks if tracks is not None else self.tracks):
            points.update(track.edit_points(self.rate))
        return sorted(points)

    def next_edit_point(self, time: RationalTime,
                        tracks: Optional[SequenceType[Track]] = None) -> Optional[RationalTime]:
        return next((p for p in self.edit_points(tracks) if p > time), None)

    def previous_edit_point(self, time: RationalTime,
                            tracks: Optional[SequenceType[Track]] = None) -> Optional[RationalTime]:
        earlier = [p for p in self.edit_points(tracks) if p < time]
        return earlier[-1] if earlier else None

    # -- validation ------------------------------------------------------

    def validate(self, registry: Optional[MediaRegistry] = None) -> List[str]:
        """Return a list of invariant violations. Empty list == healthy."""
        problems: List[str] = []
        for track in self.tracks:
            ordered = sorted(track.clips, key=lambda c: c.start.to_seconds())
            if [c.clip_id for c in ordered] != [c.clip_id for c in track.clips]:
                problems.append(f"{track.name}: clips are not stored in time order")
            for a, b in zip(ordered, ordered[1:]):
                if a.range.overlaps(b.range):
                    problems.append(f"{track.name}: {a.name or a.clip_id} overlaps {b.name or b.clip_id}")
            for clip in track.clips:
                if clip.duration.value <= 0:
                    problems.append(f"{track.name}: {clip.name or clip.clip_id} has non-positive duration")
                if clip.start.value < 0:
                    problems.append(f"{track.name}: {clip.name or clip.clip_id} starts before zero")
                if clip.source_range.start_time.value < 0:
                    problems.append(f"{track.name}: {clip.name or clip.clip_id} reads before the start of its media")
                if registry is not None:
                    if clip.media_id not in registry:
                        problems.append(f"{track.name}: {clip.name or clip.clip_id} references unknown media {clip.media_id}")
                        continue
                    ref = registry.get(clip.media_id)
                    info = ref.info
                    if info is None:
                        continue  # offline is a state, not a violation
                    if info.duration_seconds is not None:
                        limit = info.duration(self.rate)
                        if clip.source_range.end_time_exclusive > limit:
                            problems.append(
                                f"{track.name}: {clip.name or clip.clip_id} reads past the end of "
                                f"{ref.name} ({clip.source_range.end_time_exclusive} > {limit})")
        return problems

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence_id": self.sequence_id, "name": self.name,
            "rate": str(self.rate), "width": self.width, "height": self.height,
            "sample_rate": self.sample_rate, "channels": self.channels,
            "background_color": self.background_color,
            "in_point": _time_to_dict(self.in_point) if self.in_point else None,
            "out_point": _time_to_dict(self.out_point) if self.out_point else None,
            "video_tracks": [t.to_dict() for t in self.video_tracks],
            "audio_tracks": [t.to_dict() for t in self.audio_tracks],
            "markers": [m.to_dict() for m in self.markers],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Sequence":
        seq = cls(
            sequence_id=data["sequence_id"], name=data.get("name", "Sequence"),
            rate=Fraction(data.get("rate", "30")),
            width=data.get("width", 1920), height=data.get("height", 1080),
            sample_rate=data.get("sample_rate", 48000), channels=data.get("channels", 2),
            background_color=data.get("background_color", "black"),
            video_tracks=[Track.from_dict(t) for t in data.get("video_tracks", [])],
            audio_tracks=[Track.from_dict(t) for t in data.get("audio_tracks", [])],
            markers=[Marker.from_dict(m) for m in data.get("markers", [])],
        )
        seq.in_point = _time_from_dict(data["in_point"]) if data.get("in_point") else None
        seq.out_point = _time_from_dict(data["out_point"]) if data.get("out_point") else None
        return seq


@dataclass
class Project:
    """Media + sequences + settings: everything a .json project file holds."""

    name: str = "Untitled"
    project_id: str = field(default_factory=lambda: new_id("prj"))
    registry: MediaRegistry = field(default_factory=MediaRegistry)
    sequences: List[Sequence] = field(default_factory=list)
    active_sequence_id: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=dict)

    # -- sequences -------------------------------------------------------

    def add_sequence(self, sequence: Sequence, make_active: bool = True) -> Sequence:
        self.sequences.append(sequence)
        if make_active or self.active_sequence_id is None:
            self.active_sequence_id = sequence.sequence_id
        return sequence

    def sequence(self, sequence_id: Optional[str] = None) -> Sequence:
        target = sequence_id or self.active_sequence_id
        for seq in self.sequences:
            if seq.sequence_id == target:
                return seq
        raise ModelError(f"unknown sequence {target!r}")

    @property
    def active_sequence(self) -> Sequence:
        return self.sequence()

    # -- media -----------------------------------------------------------

    def import_media(self, path, name: str = "") -> MediaRef:
        return self.registry.add(path, name=name)

    def media_for(self, clip: Clip) -> MediaRef:
        return self.registry.get(clip.media_id)

    def validate(self) -> List[str]:
        problems: List[str] = []
        for seq in self.sequences:
            problems.extend(f"[{seq.name}] {p}" for p in seq.validate(self.registry))
        return problems

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project_id, "name": self.name,
            "active_sequence_id": self.active_sequence_id,
            "settings": dict(self.settings),
            "registry": self.registry.to_dict(),
            "sequences": [s.to_dict() for s in self.sequences],
        }

    def save(self, path: str | Path) -> Path:
        """Write the project to disk atomically.

        Atomically, because a project file truncated by a crash mid-write is
        every edit ever made on it, gone.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        os.replace(temporary, path)
        self.registry.save_cache()
        return path

    @classmethod
    def load(cls, path: str | Path, cache_path=None) -> "Project":
        """Read a project back. Missing media loads as offline, not as an error."""
        path = Path(path)
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")),
                             cache_path=cache_path)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], cache_path=None) -> "Project":
        version = data.get("schema_version", 0)
        if version > SCHEMA_VERSION:
            raise ModelError(
                f"project was written by a newer engine (schema {version} > {SCHEMA_VERSION})")
        return cls(
            name=data.get("name", "Untitled"),
            project_id=data.get("project_id", new_id("prj")),
            registry=MediaRegistry.from_dict(data.get("registry", {}), cache_path=cache_path),
            sequences=[Sequence.from_dict(s) for s in data.get("sequences", [])],
            active_sequence_id=data.get("active_sequence_id"),
            settings=dict(data.get("settings", {})),
        )


# ---------------------------------------------------------------------------
# time (de)serialization -- exact, never through float
# ---------------------------------------------------------------------------

def _time_to_dict(time: RationalTime) -> Dict[str, str]:
    return {"value": str(time.value), "rate": str(time.rate)}


def _time_from_dict(data: Dict[str, str]) -> RationalTime:
    return RationalTime(Fraction(data["value"]), Fraction(data["rate"]))


def _range_to_dict(span: TimeRange) -> Dict[str, Any]:
    return {"start_time": _time_to_dict(span.start_time),
            "duration": _time_to_dict(span.duration)}


def _range_from_dict(data: Dict[str, Any]) -> TimeRange:
    return TimeRange(_time_from_dict(data["start_time"]), _time_from_dict(data["duration"]))


def make_clip(media: MediaRef, sequence: Sequence, start: RationalTime,
              source_in: Optional[RationalTime] = None,
              duration: Optional[RationalTime] = None,
              name: str = "", link_id: Optional[str] = None,
              speed: Fraction = Fraction(1)) -> Clip:
    """Build a clip against a media ref, clamped to what the media actually has.

    Defaults to the whole file from its head, which is what dragging a clip
    onto a timeline does.
    """
    info = media.require_info()
    rate = sequence.rate
    source_in = source_in or RationalTime.zero(rate)
    if duration is None:
        if info.duration_seconds is None:
            raise ModelError(
                f"{media.name} is a still image; give make_clip an explicit duration")
        duration = info.duration(rate) - source_in
    if duration.value <= 0:
        raise ModelError(f"clip duration must be positive (got {duration})")
    if info.duration_seconds is not None:
        limit = info.duration(rate)
        if source_in + duration * abs(to_fraction(speed)) > limit:
            duration = (limit - source_in) / abs(to_fraction(speed))
            if duration.value <= 0:
                raise ModelError(f"source in point {source_in} is at or past the end of {media.name}")
    return Clip(
        clip_id=new_id("clip"), media_id=media.media_id,
        source_range=TimeRange(source_in, duration * abs(to_fraction(speed))),
        start=start, name=name or media.name, link_id=link_id, speed=to_fraction(speed),
    )

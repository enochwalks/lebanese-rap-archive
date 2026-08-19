"""
plan.py -- compile a timeline into a backend-independent render plan.

This is deliberately a *pure function*: Sequence in, RenderPlan out, no
processes spawned, no files touched. Three reasons that split is worth the
extra type:

* It is unit-testable. Asserting "clip B is composited from 3.0s to 6.0s on
  layer 2 at 0.5x" needs no ffmpeg, no media and no waiting, so the hard part
  (the timing maths) is covered by fast tests.
* The backend is replaceable. Today it is ffmpeg-per-render; a GPU preview
  renderer or a distributed batch renderer consumes the same plan.
* It is inspectable. `plan.describe()` is what you read when a render came
  out wrong, instead of squinting at a 40 kB filtergraph.

The plan flattens tracks into layers (V1 at the bottom) and resolves every
time to exact seconds relative to the render's start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional

from ..media import MediaRegistry
from ..model import AUDIO, VIDEO, Clip, Sequence, Track
from ..timebase import TimeRange


class RenderPlanError(RuntimeError):
    pass


@dataclass
class RenderSettings:
    """Output format. Defaults target a YouTube-friendly 1080p H.264 file."""

    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 18
    preset: str = "medium"
    pixel_format: str = "yuv420p"
    audio_bitrate: str = "192k"
    faststart: bool = True
    extra_output_args: List[str] = field(default_factory=list)
    #: scale mode for clips whose raster differs from the sequence:
    #: "fit" letterboxes, "fill" crops to fill, "stretch" distorts.
    scale_mode: str = "fit"
    threads: int = 0                  # 0 = let ffmpeg decide
    hardware_accel: Optional[str] = None   # e.g. "cuda", "videotoolbox"


@dataclass
class InputSpec:
    """One decoder instance: a file plus the window to read from it."""

    index: int
    path: str
    source_start: Fraction            # seconds into the file
    source_duration: Fraction         # seconds to read
    is_still: bool = False
    has_video: bool = False
    has_audio: bool = False
    media_id: str = ""


@dataclass
class VideoSegment:
    input_index: int
    timeline_start: Fraction          # seconds, relative to render start
    timeline_duration: Fraction
    layer: int                        # 0 = bottom
    speed: Fraction = Fraction(1)
    source_width: int = 0
    source_height: int = 0
    clip_id: str = ""
    clip_name: str = ""
    effects: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def timeline_end(self) -> Fraction:
        return self.timeline_start + self.timeline_duration

    @property
    def is_reversed(self) -> bool:
        return self.speed < 0


@dataclass
class AudioSegment:
    input_index: int
    timeline_start: Fraction
    timeline_duration: Fraction
    gain_db: float = 0.0
    speed: Fraction = Fraction(1)
    clip_id: str = ""
    clip_name: str = ""

    @property
    def timeline_end(self) -> Fraction:
        return self.timeline_start + self.timeline_duration


@dataclass
class RenderPlan:
    width: int
    height: int
    frame_rate: Fraction
    sample_rate: int
    channels: int
    duration: Fraction                # seconds
    inputs: List[InputSpec] = field(default_factory=list)
    video: List[VideoSegment] = field(default_factory=list)
    audio: List[AudioSegment] = field(default_factory=list)
    background_color: str = "black"
    settings: RenderSettings = field(default_factory=RenderSettings)
    warnings: List[str] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return int(self.duration * self.frame_rate)

    def describe(self) -> str:
        """Human-readable render plan -- the first thing to read when a render
        comes out wrong."""
        lines = [
            f"RenderPlan {self.width}x{self.height} @ {self.frame_rate} fps, "
            f"{float(self.duration):.3f}s ({self.frame_count} frames), "
            f"{len(self.inputs)} inputs",
        ]
        for segment in sorted(self.video, key=lambda s: (s.layer, s.timeline_start)):
            speed = "" if segment.speed == 1 else f" @{float(segment.speed):g}x"
            lines.append(
                f"  V L{segment.layer} [{float(segment.timeline_start):8.3f} -> "
                f"{float(segment.timeline_end):8.3f}] {segment.clip_name}{speed}")
        for segment in sorted(self.audio, key=lambda s: s.timeline_start):
            gain = "" if segment.gain_db == 0 else f" {segment.gain_db:+.1f}dB"
            lines.append(
                f"  A    [{float(segment.timeline_start):8.3f} -> "
                f"{float(segment.timeline_end):8.3f}] {segment.clip_name}{gain}")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)


def _active_tracks(sequence: Sequence, kind: str) -> List[Track]:
    stack = sequence.video_tracks if kind == VIDEO else sequence.audio_tracks
    tracks = [t for t in stack if t.enabled]
    if kind == AUDIO:
        soloed = [t for t in tracks if t.solo]
        if soloed:
            tracks = soloed          # solo wins over mute, as everywhere else
        tracks = [t for t in tracks if not t.muted]
    return tracks


def compile_plan(sequence: Sequence, registry: MediaRegistry,
                 render_range: Optional[TimeRange] = None,
                 settings: Optional[RenderSettings] = None) -> RenderPlan:
    """Flatten a sequence into a render plan.

    `render_range` defaults to the sequence's work area (its in/out points, or
    the whole thing). Clips are clipped to that window, so rendering a
    selection never re-times anything -- it just shows less of it.
    """
    settings = settings or RenderSettings()
    span = render_range or sequence.work_range
    if span.is_empty:
        span = sequence.range

    plan = RenderPlan(
        width=sequence.width, height=sequence.height, frame_rate=sequence.rate,
        sample_rate=sequence.sample_rate, channels=sequence.channels,
        duration=span.duration.to_seconds(), background_color=sequence.background_color,
        settings=settings,
    )

    offline: List[str] = []
    inputs_by_key: Dict[Any, InputSpec] = {}

    def add_input(clip: Clip, source: TimeRange, is_still: bool,
                  has_video: bool, has_audio: bool, path: str) -> InputSpec:
        # One decoder per (media, source window, stream kind). Two clips using
        # the exact same window share a decoder; anything else gets its own,
        # because ffmpeg reads each input linearly.
        key = (path, source.start_time.to_seconds(), source.duration.to_seconds(),
               has_video, has_audio)
        existing = inputs_by_key.get(key)
        if existing is not None:
            return existing
        spec = InputSpec(
            index=len(plan.inputs), path=path,
            source_start=source.start_time.to_seconds(),
            source_duration=source.duration.to_seconds(),
            is_still=is_still, has_video=has_video, has_audio=has_audio,
            media_id=clip.media_id,
        )
        plan.inputs.append(spec)
        inputs_by_key[key] = spec
        return spec

    def visible_part(clip: Clip) -> Optional[tuple[TimeRange, TimeRange]]:
        """Clip the clip to the render window; return (timeline, source)."""
        visible = clip.range.intersection(span)
        if visible is None or visible.is_empty:
            return None
        head_trim = visible.start_time - clip.start
        source_start = clip.source_time_at(visible.start_time)
        source_duration = visible.duration * abs(clip.speed)
        if clip.is_reversed:
            source_start = source_start - source_duration
        return visible, TimeRange(source_start, source_duration)

    for layer, track in enumerate(_active_tracks(sequence, VIDEO)):
        for clip in track.clips:
            if not clip.enabled:
                continue
            parts = visible_part(clip)
            if parts is None:
                continue
            visible, source = parts
            ref = registry.get(clip.media_id) if clip.media_id in registry else None
            if ref is None or not ref.online:
                offline.append(clip.name or clip.clip_id)
                continue
            info = ref.require_info()
            if not info.has_video:
                plan.warnings.append(
                    f"{clip.name or clip.clip_id} is on a video track but has no video stream")
                continue
            spec = add_input(clip, source, info.is_still, True, False, ref.path)
            video_stream = info.primary_video
            plan.video.append(VideoSegment(
                input_index=spec.index,
                timeline_start=(visible.start_time - span.start_time).to_seconds(),
                timeline_duration=visible.duration.to_seconds(),
                layer=layer, speed=clip.speed,
                source_width=video_stream.width if video_stream else 0,
                source_height=video_stream.height if video_stream else 0,
                clip_id=clip.clip_id, clip_name=clip.name or clip.clip_id,
                effects=[e.to_dict() for e in clip.effects if e.enabled],
            ))

    for track in _active_tracks(sequence, AUDIO):
        for clip in track.clips:
            if not clip.enabled:
                continue
            parts = visible_part(clip)
            if parts is None:
                continue
            visible, source = parts
            ref = registry.get(clip.media_id) if clip.media_id in registry else None
            if ref is None or not ref.online:
                offline.append(clip.name or clip.clip_id)
                continue
            info = ref.require_info()
            if not info.has_audio:
                continue             # a picture-only clip on an audio track is silent
            spec = add_input(clip, source, False, False, True, ref.path)
            plan.audio.append(AudioSegment(
                input_index=spec.index,
                timeline_start=(visible.start_time - span.start_time).to_seconds(),
                timeline_duration=visible.duration.to_seconds(),
                gain_db=clip.gain_db + track.volume_db,
                speed=clip.speed,
                clip_id=clip.clip_id, clip_name=clip.name or clip.clip_id,
            ))

    if offline:
        # Fail before starting, not three hours into a batch.
        raise RenderPlanError(
            "cannot render: media is offline for " + ", ".join(sorted(set(offline))))

    for segment in plan.video:
        if segment.is_reversed:
            plan.warnings.append(
                f"{segment.clip_name} plays in reverse; ffmpeg buffers the whole "
                f"clip to do that, so keep reversed shots short")
    return plan

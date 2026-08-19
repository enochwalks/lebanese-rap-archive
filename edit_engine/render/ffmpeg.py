"""
ffmpeg.py -- the ffmpeg backend: RenderPlan -> command line -> file.

FILTERGRAPH STRATEGY (and why it is not the obvious one)
--------------------------------------------------------
The obvious way to render a timeline with ffmpeg is one `overlay` per clip,
chained. It is easy to write and it collapses on real projects: every overlay
in the chain processes the *entire* output duration, so a 200-cut beat edit on
a 3-minute song makes ffmpeg push about a million frame-pairs through overlay
filters that are no-ops for 99.5% of their length.

So instead each track is flattened first:

    layer = concat(clip, gap, clip, clip, gap, ...)      -- one stream per track
    output = overlay(overlay(layer0, layer1), layer2)    -- one overlay per track

Gaps become generated colour segments (opaque background on the bottom track,
fully transparent above it), so concat always sees a contiguous timeline. Cost
goes from O(clips) full-length passes to O(tracks) -- a 200-cut single-track
edit becomes one concat and zero overlays.

Every segment is normalised to the sequence raster and frame rate *before*
concat (`fps`, `scale`, `pad`, `setsar`, `format`), because concat demands
matching parameters and silently misbehaves otherwise.

Audio is delayed in **samples**, not milliseconds (`adelay=...S`), so a J-cut
lands where the model says it lands rather than at the nearest millisecond.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, List, Optional, Sequence as Seq, Tuple

from .effects import EffectContext, compile_effects
from .plan import AudioSegment, RenderPlan, VideoSegment

FFMPEG = os.environ.get("FFMPEG_BINARY", "ffmpeg")

ProgressCallback = Callable[[float, float], None]   # (seconds_done, seconds_total)


class RenderError(RuntimeError):
    def __init__(self, message: str, stderr: str = "", command: Optional[Seq[str]] = None):
        super().__init__(message)
        self.stderr = stderr
        self.command = list(command or [])


def _s(value) -> str:
    """Format an exact time for ffmpeg without throwing away precision."""
    return f"{float(value):.9f}"


def _channel_layout(channels: int) -> str:
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}c")


def _db_to_linear(db: float) -> float:
    return 10 ** (db / 20.0)


def _atempo_chain(factor: Fraction) -> List[str]:
    """atempo only accepts 0.5-100 per instance, so extreme retimes chain."""
    filters: List[str] = []
    value = float(abs(factor))
    if value <= 0:
        return filters
    while value > 100:
        filters.append("atempo=100")
        value /= 100
    while value < 0.5:
        filters.append("atempo=0.5")
        value /= 0.5
    if abs(value - 1.0) > 1e-9:
        filters.append(f"atempo={value:.9f}")
    return filters


def _scale_filters(plan: RenderPlan) -> str:
    """Conform any raster to the sequence raster."""
    width, height = plan.width, plan.height
    mode = plan.settings.scale_mode
    if mode == "stretch":
        return f"scale={width}:{height}"
    if mode == "fill":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}")
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")


def _video_segment_chain(plan: RenderPlan, segment: VideoSegment, label: str,
                         pixel_format: str) -> str:
    """Normalise one clip to the sequence raster, rate and exact duration."""
    parts = [f"[{segment.input_index}:v]"]
    filters = []
    if segment.is_reversed:
        filters.append("reverse")
    speed = abs(segment.speed)
    if speed != 1:
        filters.append(f"setpts=PTS/{float(speed):.9f}")
    filters.append(f"fps={plan.frame_rate}")
    filters.append(_scale_filters(plan))
    filters.append("setsar=1")
    # Effects run in output-pixel space, after the clip is conformed, so they
    # compose the same way regardless of what raster the source was shot at.
    if segment.effects:
        context = EffectContext(plan.width, plan.height, plan.frame_rate,
                                segment.timeline_duration, is_still=segment.is_still)
        effect_filters, warnings = compile_effects(segment.effects, context)
        filters.extend(effect_filters)
        for warning in warnings:
            if warning not in plan.warnings:
                plan.warnings.append(f"{segment.clip_name}: {warning}")
    filters.append(f"format={pixel_format}")
    # Exact length: trim decides how many frames this segment contributes, and
    # tpad tops up if the source ran short, so concat can never desync.
    filters.append(f"trim=duration={_s(segment.timeline_duration)}")
    filters.append("setpts=PTS-STARTPTS")
    filters.append(f"tpad=stop_mode=clone:stop_duration={_s(segment.timeline_duration)}")
    filters.append(f"trim=duration={_s(segment.timeline_duration)}")
    filters.append("setpts=PTS-STARTPTS")
    parts.append(",".join(filters))
    parts.append(f"[{label}]")
    return "".join(parts)


def _gap_chain(plan: RenderPlan, duration: Fraction, label: str,
               color: str, pixel_format: str) -> str:
    return (f"color=c={color}:s={plan.width}x{plan.height}:r={plan.frame_rate}:"
            f"d={_s(duration)},format={pixel_format},setsar=1[{label}]")


def _build_video_graph(plan: RenderPlan) -> Tuple[List[str], str]:
    """Returns (filter statements, final video label)."""
    statements: List[str] = []
    layers = sorted({segment.layer for segment in plan.video})
    layer_labels: List[str] = []

    for position, layer in enumerate(layers):
        bottom = position == 0
        pixel_format = "yuv420p" if bottom else "yuva420p"
        gap_color = plan.background_color if bottom else "black@0.0"
        segments = sorted((s for s in plan.video if s.layer == layer),
                          key=lambda s: s.timeline_start)

        parts: List[str] = []
        cursor = Fraction(0)
        for index, segment in enumerate(segments):
            if segment.timeline_start > cursor:
                label = f"l{layer}g{index}"
                statements.append(_gap_chain(plan, segment.timeline_start - cursor,
                                             label, gap_color, pixel_format))
                parts.append(label)
            label = f"l{layer}c{index}"
            statements.append(_video_segment_chain(plan, segment, label, pixel_format))
            parts.append(label)
            cursor = segment.timeline_end
        if cursor < plan.duration:
            label = f"l{layer}gz"
            statements.append(_gap_chain(plan, plan.duration - cursor, label,
                                         gap_color, pixel_format))
            parts.append(label)

        layer_label = f"layer{layer}"
        if len(parts) == 1:
            statements.append(f"[{parts[0]}]null[{layer_label}]")
        else:
            joined = "".join(f"[{p}]" for p in parts)
            statements.append(f"{joined}concat=n={len(parts)}:v=1:a=0[{layer_label}]")
        layer_labels.append(layer_label)

    if not layer_labels:
        statements.append(
            f"color=c={plan.background_color}:s={plan.width}x{plan.height}:"
            f"r={plan.frame_rate}:d={_s(plan.duration)},format=yuv420p,setsar=1[vbase]")
        layer_labels = ["vbase"]

    current = layer_labels[0]
    for index, label in enumerate(layer_labels[1:], start=1):
        output = f"vov{index}"
        statements.append(f"[{current}][{label}]overlay=x=0:y=0:shortest=0:"
                          f"eof_action=pass[{output}]")
        current = output

    final = "vout"
    statements.append(f"[{current}]trim=duration={_s(plan.duration)},setpts=PTS-STARTPTS,"
                      f"format={plan.settings.pixel_format}[{final}]")
    return statements, final


def _audio_segment_chain(plan: RenderPlan, segment: AudioSegment, label: str) -> str:
    layout = _channel_layout(plan.channels)
    delay_samples = int(round(float(segment.timeline_start) * plan.sample_rate))
    filters = [
        f"aformat=sample_fmts=fltp:sample_rates={plan.sample_rate}:channel_layouts={layout}",
    ]
    if segment.speed < 0:
        filters.append("areverse")
    filters.extend(_atempo_chain(segment.speed))
    filters.append(f"atrim=duration={_s(segment.timeline_duration)}")
    filters.append("asetpts=PTS-STARTPTS")
    if segment.gain_db != 0:
        filters.append(f"volume={_db_to_linear(segment.gain_db):.9f}")
    if delay_samples > 0:
        # sample-accurate placement; `S` means samples, not milliseconds
        filters.append(f"adelay=delays={delay_samples}S:all=1")
    return f"[{segment.input_index}:a]" + ",".join(filters) + f"[{label}]"


def _build_audio_graph(plan: RenderPlan) -> Tuple[List[str], str]:
    statements: List[str] = []
    layout = _channel_layout(plan.channels)
    if not plan.audio:
        statements.append(
            f"anullsrc=r={plan.sample_rate}:cl={layout},"
            f"atrim=duration={_s(plan.duration)},asetpts=PTS-STARTPTS[aout]")
        return statements, "aout"

    labels: List[str] = []
    for index, segment in enumerate(plan.audio):
        label = f"a{index}"
        statements.append(_audio_segment_chain(plan, segment, label))
        labels.append(label)

    if len(labels) == 1:
        mixed = labels[0]
    else:
        joined = "".join(f"[{label}]" for label in labels)
        # normalize=0: mixing 4 quiet tracks must not duck them all by 12 dB.
        # dropout_transition=0: no gain ramp when a track ends.
        statements.append(f"{joined}amix=inputs={len(labels)}:normalize=0:"
                          f"duration=longest:dropout_transition=0[amix]")
        mixed = "amix"

    statements.append(f"[{mixed}]apad,atrim=duration={_s(plan.duration)},"
                      f"asetpts=PTS-STARTPTS[aout]")
    return statements, "aout"


def build_command(plan: RenderPlan, output_path: str | Path,
                  ffmpeg: str = FFMPEG) -> List[str]:
    """Compile a plan into a complete ffmpeg argv. No side effects."""
    settings = plan.settings
    command: List[str] = [ffmpeg, "-y", "-hide_banner", "-nostdin"]
    if settings.hardware_accel:
        command += ["-hwaccel", settings.hardware_accel]

    for spec in plan.inputs:
        if spec.is_still:
            command += ["-loop", "1", "-t", _s(spec.source_duration), "-i", spec.path]
            continue
        # -accurate_seek with input-level -ss: fast *and* frame-exact.
        # Read a little extra; the filtergraph decides the exact length.
        command += ["-accurate_seek", "-ss", _s(spec.source_start),
                    "-t", _s(spec.source_duration + Fraction(1, 2)), "-i", spec.path]

    video_statements, video_label = _build_video_graph(plan)
    audio_statements, audio_label = _build_audio_graph(plan)
    command += ["-filter_complex", ";".join(video_statements + audio_statements)]
    command += ["-map", f"[{video_label}]", "-map", f"[{audio_label}]"]

    command += ["-c:v", settings.video_codec]
    if settings.video_codec.startswith("libx26"):
        command += ["-preset", settings.preset, "-crf", str(settings.crf)]
    command += ["-pix_fmt", settings.pixel_format, "-r", str(plan.frame_rate)]
    command += ["-c:a", settings.audio_codec, "-b:a", settings.audio_bitrate,
                "-ar", str(plan.sample_rate), "-ac", str(plan.channels)]
    command += ["-t", _s(plan.duration)]
    if settings.threads:
        command += ["-threads", str(settings.threads)]
    if settings.faststart and str(output_path).lower().endswith((".mp4", ".mov", ".m4v")):
        command += ["-movflags", "+faststart"]
    command += list(settings.extra_output_args)
    command.append(str(output_path))
    return command


@dataclass
class RenderReport:
    output_path: Path
    duration_seconds: float
    frames: int
    command: List[str]

    def __repr__(self) -> str:
        return (f"RenderReport({self.output_path.name}, "
                f"{self.duration_seconds:.2f}s, {self.frames} frames)")


class FFmpegRenderer:
    """Executes a render plan.

    Renders go to a temporary file next to the destination and are moved into
    place only on success. A killed render therefore never leaves a truncated
    file that a resume-aware pipeline would mistake for finished work -- the
    exact failure mode that makes automated channels upload half a video.
    """

    def __init__(self, ffmpeg: str = FFMPEG):
        self.ffmpeg = ffmpeg

    def available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None

    def render(self, plan: RenderPlan, output_path: str | Path,
               progress: Optional[ProgressCallback] = None,
               dry_run: bool = False) -> RenderReport:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if dry_run:
            command = build_command(plan, output_path, self.ffmpeg)
            return RenderReport(output_path, float(plan.duration), plan.frame_count, command)

        if not self.available():
            raise RenderError(f"{self.ffmpeg} not found on PATH")

        handle, temp_name = tempfile.mkstemp(dir=str(output_path.parent),
                                             prefix=f".{output_path.stem}-",
                                             suffix=output_path.suffix)
        os.close(handle)
        temp_path = Path(temp_name)
        # mkstemp creates 0600; a rendered video that only the render user can
        # read breaks every downstream step (upload, review, sync). Restore the
        # permissions an ordinary file would have got.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(temp_path, 0o666 & ~umask)
        command = build_command(plan, temp_path, self.ffmpeg)

        try:
            returncode, stderr = self._run(command, float(plan.duration), progress)
            if returncode != 0:
                raise RenderError(
                    f"ffmpeg exited with {returncode} while rendering {output_path.name}",
                    stderr=stderr, command=command)
            os.replace(temp_path, output_path)   # atomic within a filesystem
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

        return RenderReport(output_path, float(plan.duration), plan.frame_count, command)

    def _run(self, command: Seq[str], total_seconds: float,
             progress: Optional[ProgressCallback]) -> Tuple[int, str]:
        full = [command[0], "-progress", "pipe:1"] + list(command[1:])
        process = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        stderr_lines: List[str] = []

        def drain() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line)

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()

        pattern = re.compile(r"out_time_us=(\d+)")
        assert process.stdout is not None
        for line in process.stdout:
            match = pattern.search(line)
            if match and progress is not None:
                progress(int(match.group(1)) / 1_000_000, total_seconds)
        process.wait()
        thread.join(timeout=5)
        if progress is not None:
            progress(total_seconds, total_seconds)
        return process.returncode, "".join(stderr_lines)

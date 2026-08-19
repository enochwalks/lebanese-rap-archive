"""
edl.py -- CMX3600 EDL export.

An automated edit that a human cannot open and finish is a dead end. The
native project JSON is the lossless format, but nothing else reads it; a
CMX3600 EDL is understood by Resolve, Premiere, Avid and Final Cut, so a cut
this engine generates can be handed to an editor, refined by hand, and taken
onward. That is a deliberate one-way door out of the automation.

What EDL cannot carry (effects, keyframes, multi-layer compositing, speed
ramps) is listed in the export's warnings rather than silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence as Seq

from .media import MediaRegistry
from .model import AUDIO, VIDEO, Sequence, Track
from .timebase import frames_to_timecode

MAX_REEL = 8


@dataclass
class EDLExport:
    text: str
    events: int
    warnings: List[str] = field(default_factory=list)

    def write(self, path) -> None:
        from pathlib import Path
        Path(path).write_text(self.text, encoding="utf-8")


def _reel_name(name: str, used: dict) -> str:
    """EDL reel names are 8 characters of A-Z0-9; keep them unique."""
    cleaned = re.sub(r"[^A-Z0-9]", "", name.upper())[:MAX_REEL] or "AX"
    if cleaned in used and used[cleaned] != name:
        suffix = 1
        while f"{cleaned[:MAX_REEL - len(str(suffix))]}{suffix}" in used:
            suffix += 1
        cleaned = f"{cleaned[:MAX_REEL - len(str(suffix))]}{suffix}"
    used[cleaned] = name
    return cleaned


def _channel(track: Track, audio_index: int) -> str:
    if track.kind == VIDEO:
        return "V"
    return "A" if audio_index == 0 else f"A{audio_index + 1}"


def to_edl(sequence: Sequence, registry: Optional[MediaRegistry] = None,
           title: Optional[str] = None, tracks: Optional[Seq[Track]] = None,
           drop_frame: Optional[bool] = None) -> EDLExport:
    """Export a sequence as CMX3600 text.

    Only the first video track is exported by default: the format has no
    concept of a layer stack, and quietly flattening one would produce an EDL
    that does not match the timeline.
    """
    rate = sequence.rate
    if drop_frame is None:
        drop_frame = rate.denominator == 1001 and rate.numerator in (30000, 60000)

    if tracks is None:
        tracks = ([sequence.video_tracks[0]] if sequence.video_tracks else []) + \
                 list(sequence.audio_tracks)

    warnings: List[str] = []
    if len(sequence.video_tracks) > 1 and any(t.clips for t in sequence.video_tracks[1:]):
        warnings.append(
            "EDL carries one video track; upper tracks were not exported "
            "(use the project JSON for a lossless hand-off)")

    lines = [
        f"TITLE: {(title or sequence.name)[:70]}",
        f"FCM: {'DROP FRAME' if drop_frame else 'NON-DROP FRAME'}",
    ]

    reels: dict = {}
    retimed: List[str] = []
    effected = 0
    audio_index = 0
    event = 0
    for track in tracks:
        channel = _channel(track, audio_index)
        if track.kind == AUDIO:
            audio_index += 1
        for clip in track.clips:
            if not clip.enabled:
                continue
            event += 1
            name = clip.name or clip.clip_id
            if registry is not None and clip.media_id in registry:
                name = registry.get(clip.media_id).name or name
            reel = _reel_name(name, reels)

            source_in = clip.source_range.start_time.to_frames(rate)
            source_out = clip.source_range.end_time_exclusive.to_frames(rate)
            record_in = clip.start.to_frames(rate)
            record_out = clip.end.to_frames(rate)

            if clip.speed != 1:
                retimed.append(name)
            if clip.effects:
                effected += len(clip.effects)

            timecodes = " ".join(frames_to_timecode(value, rate, drop_frame)
                                 for value in (source_in, source_out, record_in, record_out))
            lines.append(f"{event:03d}  {reel:<8} {channel:<5} C        {timecodes}")
            lines.append(f"* FROM CLIP NAME: {name}")
            if clip.speed != 1:
                lines.append(f"M2   {reel:<8} {float(abs(clip.speed) * float(rate)):>+011.4f}"
                             f" {frames_to_timecode(source_in, rate, drop_frame)}")
            if registry is not None and clip.media_id in registry:
                lines.append(f"* SOURCE FILE: {registry.get(clip.media_id).path}")

    # One line per class of loss, not one per clip: a 200-cut edit would
    # otherwise bury the real warnings under 200 identical ones.
    if effected:
        warnings.append(f"{effected} effect(s) across the edit are not carried by EDL")
    if retimed:
        warnings.append(f"{len(retimed)} retimed clip(s) exported as M2 rate comments; "
                        f"verify speeds after import")

    return EDLExport("\n".join(lines) + "\n", event, warnings)

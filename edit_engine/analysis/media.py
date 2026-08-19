"""
media.py -- look at the footage.

The single biggest weakness of the first shot-picker was that it chose a
*random* moment inside each source clip. Real footage is not uniformly
interesting: it has black frames, slates, slow bits and the actual action.
Cutting at random lands on the boring parts often enough to make an edit feel
lifeless, and no amount of beat accuracy fixes that.

This module measures, using ffmpeg only (no numpy, no ML):

    motion    signalstats YDIF, the mean temporal difference between frames --
              a genuine "is something moving" signal
    luma      YAVG brightness -- finds black frames and blown-out ones
    colour    SATAVG saturation -- flat grey slates vs rich imagery
    scenes    hard cuts already present inside the source

    (An earlier version used the frame-to-frame change in *mean brightness* as
    a motion proxy. It does not work: a picture can change completely while
    its average brightness stays fixed, so busy footage measured as static and
    only fades and cuts registered. YDIF measures the actual pixel difference.)

From those it can answer the question the editor actually asks:
"where in this clip is the best N seconds to use?"

Everything is cached, because a 4K clip takes real time to scan.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cache import AnalysisCache

ANALYSER_VERSION = 1
#: analysis is done on a tiny proxy -- motion and brightness survive
#: downscaling, and scanning 4K at full size would dominate build time
PROBE_WIDTH = 160
PROBE_FPS = 4


@dataclass
class ClipAnalysis:
    """What we measured about one source file."""

    path: str
    duration: float
    #: one entry per probe frame, in order
    motion: List[float] = field(default_factory=list)
    luma: List[float] = field(default_factory=list)
    saturation: List[float] = field(default_factory=list)
    scene_cuts: List[float] = field(default_factory=list)
    fps: float = PROBE_FPS

    @property
    def mean_motion(self) -> float:
        return sum(self.motion) / len(self.motion) if self.motion else 0.0

    @property
    def mean_luma(self) -> float:
        return sum(self.luma) / len(self.luma) if self.luma else 0.0

    @property
    def mean_saturation(self) -> float:
        return sum(self.saturation) / len(self.saturation) if self.saturation else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "duration": self.duration, "fps": self.fps,
            "motion": [round(v, 5) for v in self.motion],
            "luma": [round(v, 4) for v in self.luma],
            "saturation": [round(v, 4) for v in self.saturation],
            "scene_cuts": [round(v, 3) for v in self.scene_cuts],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClipAnalysis":
        return cls(path=data["path"], duration=data["duration"],
                   fps=data.get("fps", PROBE_FPS), motion=data.get("motion", []),
                   luma=data.get("luma", []), saturation=data.get("saturation", []),
                   scene_cuts=data.get("scene_cuts", []))

    # -- the question the editor asks ------------------------------------

    def window_score(self, start: float, length: float,
                     prefer: str = "action") -> float:
        """Score a candidate window of this clip. Higher is better.

        `prefer`:
          "action" -- busy, well-exposed footage (fight scenes, fast cuts)
          "calm"   -- steady, well-exposed footage (landscapes, long holds)
        """
        first = int(start * self.fps)
        last = max(first + 1, int((start + length) * self.fps))
        motion = self.motion[first:last]
        luma = self.luma[first:last]
        if not motion or not luma:
            return 0.0

        mean_motion = sum(motion) / len(motion)
        mean_luma = sum(luma) / len(luma)

        # Penalise black frames, blown-out frames and fades hard: they are
        # never what you meant to cut to.
        if mean_luma < 0.06 or mean_luma > 0.97:
            return 0.0
        exposure = 1.0 - abs(mean_luma - 0.45) * 1.2

        # YDIF/255 is typically ~0.00 for a locked-off shot, ~0.02 for gentle
        # movement and ~0.08+ for fast action, so 25x maps the useful range
        # onto 0..1 without saturating on everything that moves at all.
        busy = min(mean_motion * 25.0, 1.0)
        if prefer == "calm":
            activity = 1.0 - busy
            if mean_motion < 0.0015:
                activity *= 0.3          # a frozen frame is not a calm shot
        else:
            activity = busy

        # A window that straddles a hard cut in the source shows two shots in
        # one, which reads as a mistake.
        straddles = any(start < cut < start + length for cut in self.scene_cuts)
        penalty = 0.45 if straddles else 1.0

        return max(0.0, activity * 0.7 + exposure * 0.3) * penalty

    def best_window(self, length: float, prefer: str = "action",
                    step: float = 0.5, avoid: Optional[List[float]] = None,
                    min_separation: float = 2.0) -> Tuple[float, float]:
        """Best in-point for a window of `length` seconds. Returns (start, score).

        `avoid` lists in-points already used from this clip, so a montage does
        not keep returning to the same two seconds of footage.
        """
        if self.duration <= length:
            return 0.0, self.window_score(0.0, length, prefer)
        avoid = avoid or []
        best_start, best_score = 0.0, -1.0
        position = 0.0
        limit = self.duration - length
        while position <= limit:
            score = self.window_score(position, length, prefer)
            if any(abs(position - used) < min_separation for used in avoid):
                score *= 0.25
            if score > best_score:
                best_start, best_score = position, score
            position += step
        return best_start, max(best_score, 0.0)


def _run(command: List[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return (result.stdout or "") + (result.stderr or "")


def analyse_clip(path: str | Path, cache: Optional[AnalysisCache] = None,
                 ffmpeg: str = "ffmpeg") -> ClipAnalysis:
    """Measure motion, brightness and internal cuts for one source file."""
    path = Path(path)

    def compute() -> Dict[str, Any]:
        return _measure(path, ffmpeg).to_dict()

    if cache is not None:
        key = AnalysisCache.key(path, "clip_motion", ANALYSER_VERSION)
        return ClipAnalysis.from_dict(cache.get_or_compute(key, compute))
    return ClipAnalysis.from_dict(compute())


def _measure(path: Path, ffmpeg: str) -> ClipAnalysis:
    duration = _duration(path)

    # One downscaled, low-frame-rate pass gives every signal we need:
    #   YAVG   mean luma        -> exposure, black frames
    #   YDIF   temporal diff    -> motion
    #   SATAVG mean saturation  -> flat slates vs real imagery
    #   scdet  scene scores     -> hard cuts inside the source
    output = _run([
        ffmpeg, "-hide_banner", "-nostdin", "-i", str(path),
        "-vf", (f"fps={PROBE_FPS},scale={PROBE_WIDTH}:-2,"
                f"scdet=threshold=10,signalstats,metadata=print"),
        "-an", "-f", "null", "-",
    ])

    def series(key: str, scale: float) -> List[float]:
        return [min(1.0, float(m) / scale)
                for m in re.findall(rf"lavfi\.signalstats\.{key}=([\d.]+)", output)]

    luma = series("YAVG", 255.0)
    motion = series("YDIF", 255.0)
    saturation = series("SATAVG", 255.0)

    scene_cuts = [float(m) for m in re.findall(r"lavfi\.scd\.time=([\d.]+)", output)]

    # Smooth motion over ~1s so one flash frame does not read as sustained action.
    window = max(1, PROBE_FPS // 2)
    smoothed: List[float] = []
    for index in range(len(motion)):
        chunk = motion[max(0, index - window):index + window + 1]
        smoothed.append(sum(chunk) / len(chunk) if chunk else 0.0)

    return ClipAnalysis(path=str(path.resolve()), duration=duration,
                        motion=smoothed, luma=luma, saturation=saturation,
                        scene_cuts=scene_cuts, fps=PROBE_FPS)


def _duration(path: Path) -> float:
    output = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "json", str(path)])
    try:
        return float(json.loads(output)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def sample_frames(path: str | Path, count: int = 3, width: int = 512,
                  ffmpeg: str = "ffmpeg",
                  at: Optional[List[float]] = None) -> List[bytes]:
    """Extract representative JPEG frames, for showing a source to a model.

    Defaults to evenly spaced samples, skipping the very start and end where
    fades and titles live.
    """
    path = Path(path)
    duration = _duration(path)
    if at is None:
        if duration <= 0:
            at = [0.0]
        else:
            span = duration * 0.8
            start = duration * 0.1
            at = [start + span * i / max(1, count - 1) for i in range(count)] \
                if count > 1 else [duration / 2]

    frames: List[bytes] = []
    for timestamp in at:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-accurate_seek",
             "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg",
             "-q:v", "4", "-"],
            capture_output=True)
        if result.returncode == 0 and result.stdout:
            frames.append(result.stdout)
    return frames

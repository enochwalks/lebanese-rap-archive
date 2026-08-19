"""
music.py -- listen to the song.

Beats say *where* a cut may land. Energy says *what the cut should be*: the
same 120 bpm track has a quiet intro and a loud drop, and cutting both the
same way is what makes an automated edit feel mechanical no matter how
accurate its beat detection is.

Beat detection needs librosa (optional). The energy envelope does not -- it is
computed by decoding the song to low-rate mono through ffmpeg and taking RMS
in plain Python, so energy matching works on any machine that can render at
all.
"""

from __future__ import annotations

import math
import subprocess
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cache import AnalysisCache

MUSIC_VERSION = 1
ENVELOPE_RATE = 8000       # plenty for an amplitude envelope
HOP_SECONDS = 0.25


@dataclass
class MusicAnalysis:
    path: str
    duration: float = 0.0
    tempo_bpm: Optional[float] = None
    beats: List[float] = field(default_factory=list)
    #: normalised 0..1 loudness, one value per HOP_SECONDS
    energy: List[float] = field(default_factory=list)
    hop: float = HOP_SECONDS
    beats_available: bool = False

    def energy_at(self, seconds: float) -> float:
        if not self.energy:
            return 0.5
        index = int(seconds / self.hop)
        return self.energy[max(0, min(index, len(self.energy) - 1))]

    def mean_energy_over(self, start: float, length: float) -> float:
        if not self.energy or length <= 0:
            return 0.5
        first = int(start / self.hop)
        last = max(first + 1, int((start + length) / self.hop))
        window = self.energy[first:last]
        return sum(window) / len(window) if window else 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "duration": self.duration,
                "tempo_bpm": self.tempo_bpm, "hop": self.hop,
                "beats_available": self.beats_available,
                "beats": [round(b, 4) for b in self.beats],
                "energy": [round(e, 4) for e in self.energy]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MusicAnalysis":
        return cls(path=data["path"], duration=data.get("duration", 0.0),
                   tempo_bpm=data.get("tempo_bpm"), beats=data.get("beats", []),
                   energy=data.get("energy", []), hop=data.get("hop", HOP_SECONDS),
                   beats_available=data.get("beats_available", False))


def energy_envelope(path: str | Path, hop: float = HOP_SECONDS,
                    ffmpeg: str = "ffmpeg") -> List[float]:
    """Normalised loudness over time, using ffmpeg only."""
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path),
         "-f", "s16le", "-ac", "1", "-ar", str(ENVELOPE_RATE), "-"],
        capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return []

    samples = array("h")
    samples.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    per_hop = max(1, int(ENVELOPE_RATE * hop))

    values: List[float] = []
    for start in range(0, len(samples), per_hop):
        window = samples[start:start + per_hop]
        if not window:
            break
        total = 0
        for value in window:
            total += value * value
        values.append(math.sqrt(total / len(window)))

    peak = max(values) if values else 0.0
    if peak <= 0:
        return [0.0] * len(values)
    # Normalise against a high percentile rather than the absolute peak, so one
    # transient does not squash the whole envelope.
    ordered = sorted(values)
    reference = ordered[int(len(ordered) * 0.95)] or peak
    return [min(1.0, value / reference) for value in values]


def detect_beats(path: str | Path) -> tuple[List[float], Optional[float], bool]:
    """(beat times, tempo, available). Empty and False when librosa is missing."""
    try:
        import librosa
    except ImportError:
        return [], None, False
    try:
        samples, sample_rate = librosa.load(str(path))
        tempo, frames = librosa.beat.beat_track(y=samples, sr=sample_rate)
        times = [float(t) for t in librosa.frames_to_time(frames, sr=sample_rate)]
        tempo_value = float(tempo) if not hasattr(tempo, "__len__") else float(tempo[0])
        return times, round(tempo_value, 1), True
    except Exception:                                    # noqa: BLE001
        return [], None, False


def analyse_music(path: str | Path, cache: Optional[AnalysisCache] = None) -> MusicAnalysis:
    path = Path(path)

    def compute() -> Dict[str, Any]:
        beats, tempo, available = detect_beats(path)
        energy = energy_envelope(path)
        duration = len(energy) * HOP_SECONDS
        return MusicAnalysis(path=str(path.resolve()), duration=duration,
                             tempo_bpm=tempo, beats=beats, energy=energy,
                             beats_available=available).to_dict()

    if cache is not None:
        key = AnalysisCache.key(path, "music", MUSIC_VERSION)
        return MusicAnalysis.from_dict(cache.get_or_compute(key, compute))
    return MusicAnalysis.from_dict(compute())

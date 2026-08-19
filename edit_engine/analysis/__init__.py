"""Analysis: looking at the media and listening to the music before cutting."""

from .cache import AnalysisCache
from .media import ClipAnalysis, analyse_clip, sample_frames
from .music import MusicAnalysis, analyse_music, energy_envelope
from .vision import (ClaudeDirector, ClaudeVision, EditDirection, HeuristicDirector,
                     MeasurementVision, SourceDescription, default_backends,
                     vision_available)

__all__ = [
    "AnalysisCache", "ClipAnalysis", "analyse_clip", "sample_frames",
    "MusicAnalysis", "analyse_music", "energy_envelope",
    "ClaudeDirector", "ClaudeVision", "EditDirection", "HeuristicDirector",
    "MeasurementVision", "SourceDescription", "default_backends", "vision_available",
]

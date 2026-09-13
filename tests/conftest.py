"""Shared fixtures.

Unit tests build media by hand rather than probing real files: the model and
the edit operations must be testable without ffmpeg installed and without
waiting on I/O. The render tests (test_render.py) do use real files, and skip
themselves when ffmpeg is absent.
"""

import atexit
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edit_engine.media import AudioStreamInfo, MediaInfo, MediaRef, VideoStreamInfo
from edit_engine.model import Project, Sequence, make_clip

NTSC30 = Fraction(30000, 1001)

# Fake media carries hand-written metadata but still needs a file on disk:
# "is this media online?" is answered by looking, and the render compiler
# refuses to plan a render against media that is not there.
_FAKE_DIR = Path(tempfile.mkdtemp(prefix="edit-engine-fake-"))
atexit.register(lambda: shutil.rmtree(_FAKE_DIR, ignore_errors=True))


def _placeholder(name):
    path = _FAKE_DIR / name
    if not path.exists():
        path.write_bytes(b"")
    return str(path)


def fake_video_media(project, name="src.mp4", seconds=10, fps=Fraction(30),
                     width=1920, height=1080, with_audio=True):
    """Register a media ref with hand-written metadata (no file needed)."""
    info = MediaInfo(
        path=_placeholder(name),
        duration_seconds=Fraction(seconds),
        container="mov,mp4",
        video=(VideoStreamInfo(index=0, codec="h264", width=width, height=height,
                               frame_rate=Fraction(fps)),),
        audio=(AudioStreamInfo(index=1, codec="aac", sample_rate=48000, channels=2),)
        if with_audio else (),
    )
    ref = MediaRef(media_id=f"m-{name}", path=info.path, info=info, name=name)
    return project.registry.add_ref(ref)


def fake_audio_media(project, name="song.wav", seconds=180):
    info = MediaInfo(
        path=_placeholder(name), duration_seconds=Fraction(seconds), container="wav",
        audio=(AudioStreamInfo(index=0, codec="pcm_s16le", sample_rate=48000, channels=2),),
    )
    ref = MediaRef(media_id=f"m-{name}", path=info.path, info=info, name=name)
    return project.registry.add_ref(ref)


def fake_still_media(project, name="art.jpg"):
    info = MediaInfo(
        path=_placeholder(name), duration_seconds=None, container="image2", is_still=True,
        video=(VideoStreamInfo(index=0, codec="mjpeg", width=3000, height=2000,
                               frame_rate=Fraction(1)),),
    )
    ref = MediaRef(media_id=f"m-{name}", path=info.path, info=info, name=name)
    return project.registry.add_ref(ref)


@pytest.fixture
def project():
    return Project(name="test")


@pytest.fixture
def sequence(project):
    seq = Sequence.create(name="Main", rate=Fraction(30), video_tracks=2, audio_tracks=2)
    project.add_sequence(seq)
    return seq


@pytest.fixture
def media(project):
    """10 seconds (300 frames at 30fps) of video with audio."""
    return fake_video_media(project)


@pytest.fixture
def ab_timeline(project, sequence, media):
    """Two butt-joined clips on V1, each 3s (90 frames), with 2s of handles.

    A occupies [0, 90), reading source frames 60..149.
    B occupies [90, 180), reading source frames 60..149.
    Both have 60 frames of handle at the head and 150 at the tail, so trims
    have somewhere to go in either direction.
    """
    track = sequence.video_tracks[0]
    clip_a = make_clip(media, sequence, sequence.zero(),
                       source_in=sequence.frames(60), duration=sequence.frames(90), name="A")
    clip_b = make_clip(media, sequence, sequence.frames(90),
                       source_in=sequence.frames(60), duration=sequence.frames(90), name="B")
    track.place(clip_a)
    track.place(clip_b)
    return sequence, track, clip_a, clip_b


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


requires_ffmpeg = pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg/ffprobe not on PATH")

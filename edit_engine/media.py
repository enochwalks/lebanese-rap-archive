"""
media.py -- media identification, probing and the media registry.

RULES THIS MODULE ENFORCES
--------------------------
1. Source media is *never* written to, moved or transcoded here. The registry
   opens files read-only through ffprobe and nothing else. Everything the
   engine does downstream is a description of how to read those files.
2. Probing is expensive (a process spawn per file), so results are cached on
   disk keyed by path + size + mtime. A file that changed on disk is re-probed
   automatically; a file that did not is free after the first run.
3. Media can go offline. A missing file is not an error at model-load time --
   the clips referencing it stay in the timeline, marked offline, and can be
   relinked later. Losing edits because a drive was unplugged is unacceptable.
4. Frame rates are exact Fractions taken from the container (30000/1001, not
   29.97). See timebase.py for why.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .timebase import RationalTime, TimeRange, to_fraction

FFPROBE = os.environ.get("FFPROBE_BINARY", "ffprobe")
PROBE_TIMEOUT_SECONDS = 60

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


class MediaError(RuntimeError):
    pass


class MediaOfflineError(MediaError):
    pass


@dataclass(frozen=True)
class VideoStreamInfo:
    index: int
    codec: str
    width: int
    height: int
    frame_rate: Fraction
    pixel_format: str = ""
    sample_aspect_ratio: Fraction = Fraction(1)
    rotation: int = 0
    frame_count: Optional[int] = None

    @property
    def display_width(self) -> int:
        """Width after pixel aspect ratio -- anamorphic sources are not square."""
        return int(round(self.width * float(self.sample_aspect_ratio)))

    @property
    def is_rotated_quarter_turn(self) -> bool:
        return self.rotation % 180 == 90

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "codec": self.codec,
            "width": self.width, "height": self.height,
            "frame_rate": str(self.frame_rate), "pixel_format": self.pixel_format,
            "sample_aspect_ratio": str(self.sample_aspect_ratio),
            "rotation": self.rotation, "frame_count": self.frame_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VideoStreamInfo":
        return cls(
            index=data["index"], codec=data["codec"],
            width=data["width"], height=data["height"],
            frame_rate=Fraction(data["frame_rate"]),
            pixel_format=data.get("pixel_format", ""),
            sample_aspect_ratio=Fraction(data.get("sample_aspect_ratio", "1")),
            rotation=data.get("rotation", 0), frame_count=data.get("frame_count"),
        )


@dataclass(frozen=True)
class AudioStreamInfo:
    index: int
    codec: str
    sample_rate: int
    channels: int
    channel_layout: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "codec": self.codec,
            "sample_rate": self.sample_rate, "channels": self.channels,
            "channel_layout": self.channel_layout,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AudioStreamInfo":
        return cls(index=data["index"], codec=data["codec"],
                   sample_rate=data["sample_rate"], channels=data["channels"],
                   channel_layout=data.get("channel_layout", ""))


@dataclass(frozen=True)
class MediaInfo:
    """Everything the engine knows about one source file."""

    path: str
    duration_seconds: Optional[Fraction]      # None for stills
    container: str = ""
    size_bytes: int = 0
    mtime_ns: int = 0
    video: tuple[VideoStreamInfo, ...] = ()
    audio: tuple[AudioStreamInfo, ...] = ()
    is_still: bool = False

    @property
    def has_video(self) -> bool:
        return bool(self.video)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def primary_video(self) -> Optional[VideoStreamInfo]:
        return self.video[0] if self.video else None

    @property
    def primary_audio(self) -> Optional[AudioStreamInfo]:
        return self.audio[0] if self.audio else None

    @property
    def frame_rate(self) -> Optional[Fraction]:
        v = self.primary_video
        return v.frame_rate if v else None

    def duration(self, rate) -> RationalTime:
        """Source duration expressed at `rate`. Stills report zero -- callers
        that support stills must supply their own duration."""
        if self.duration_seconds is None:
            return RationalTime.zero(rate)
        return RationalTime.from_seconds(self.duration_seconds, rate)

    def available_range(self, rate) -> TimeRange:
        """Full readable span of the file, starting at 0 (source-local time)."""
        return TimeRange(RationalTime.zero(rate), self.duration(rate))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "duration_seconds": None if self.duration_seconds is None else str(self.duration_seconds),
            "container": self.container, "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns, "is_still": self.is_still,
            "video": [v.to_dict() for v in self.video],
            "audio": [a.to_dict() for a in self.audio],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaInfo":
        dur = data.get("duration_seconds")
        return cls(
            path=data["path"],
            duration_seconds=None if dur is None else Fraction(dur),
            container=data.get("container", ""),
            size_bytes=data.get("size_bytes", 0),
            mtime_ns=data.get("mtime_ns", 0),
            video=tuple(VideoStreamInfo.from_dict(v) for v in data.get("video", [])),
            audio=tuple(AudioStreamInfo.from_dict(a) for a in data.get("audio", [])),
            is_still=data.get("is_still", False),
        )


def _parse_rate(text: Optional[str], fallback: Fraction = Fraction(0)) -> Fraction:
    if not text or text in ("0/0", "N/A"):
        return fallback
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return fallback


def _stream_rotation(stream: Dict[str, Any]) -> int:
    """Rotation from either the legacy tag or the display-matrix side data."""
    tag = stream.get("tags", {}).get("rotate")
    if tag is not None:
        try:
            return int(float(tag)) % 360
        except ValueError:
            pass
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                return int(-float(side["rotation"])) % 360
            except ValueError:
                pass
    return 0


def probe(path: str | Path, ffprobe: str = FFPROBE) -> MediaInfo:
    """Read a file's technical metadata. Raises MediaOfflineError if missing."""
    path = Path(path)
    if not path.exists():
        raise MediaOfflineError(f"media not found: {path}")

    stat = path.stat()
    is_still = path.suffix.lower() in IMAGE_SUFFIXES and path.suffix.lower() != ".gif"

    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams",
           "-of", "json", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT_SECONDS,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise MediaError(f"{ffprobe} not found on PATH; install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out on {path}") from exc

    if result.returncode != 0:
        raise MediaError(f"ffprobe failed on {path}: {result.stderr.strip()[-500:]}")

    data = json.loads(result.stdout or "{}")
    fmt = data.get("format", {})

    videos: List[VideoStreamInfo] = []
    audios: List[AudioStreamInfo] = []
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video":
            # avg_frame_rate is the honest average for VFR sources;
            # r_frame_rate is the base rate. Prefer avg, fall back to r.
            rate = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
            if rate <= 0:
                rate = Fraction(25) if not is_still else Fraction(1)
            frames = stream.get("nb_frames")
            videos.append(VideoStreamInfo(
                index=stream.get("index", 0),
                codec=stream.get("codec_name", ""),
                width=int(stream.get("width") or 0),
                height=int(stream.get("height") or 0),
                frame_rate=rate,
                pixel_format=stream.get("pix_fmt", ""),
                sample_aspect_ratio=_parse_rate(
                    (stream.get("sample_aspect_ratio") or "1:1").replace(":", "/"),
                    Fraction(1)) or Fraction(1),
                rotation=_stream_rotation(stream),
                frame_count=int(frames) if frames and str(frames).isdigit() else None,
            ))
        elif kind == "audio":
            audios.append(AudioStreamInfo(
                index=stream.get("index", 0),
                codec=stream.get("codec_name", ""),
                sample_rate=int(stream.get("sample_rate") or 48000),
                channels=int(stream.get("channels") or 2),
                channel_layout=stream.get("channel_layout", ""),
            ))

    duration: Optional[Fraction]
    if is_still:
        duration = None
    else:
        raw = fmt.get("duration")
        duration = to_fraction(float(raw)) if raw not in (None, "N/A") else None
        if duration is None:
            # Some containers only report duration per stream.
            for stream in data.get("streams", []):
                raw = stream.get("duration")
                if raw not in (None, "N/A"):
                    duration = to_fraction(float(raw))
                    break

    return MediaInfo(
        path=str(path.resolve()),
        duration_seconds=duration,
        container=fmt.get("format_name", ""),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        video=tuple(videos),
        audio=tuple(audios),
        is_still=is_still,
    )


@dataclass
class MediaRef:
    """A stable handle a clip points at.

    Clips reference media by `media_id`, never by path, so relinking a moved
    file fixes every clip in the project at once and undo/redo never has to
    rewrite thousands of path strings.
    """

    media_id: str
    path: str
    info: Optional[MediaInfo] = None
    name: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def online(self) -> bool:
        return self.info is not None and Path(self.path).exists()

    def require_info(self) -> MediaInfo:
        if self.info is not None:
            return self.info
        # Distinguish "you gave me a path that does not exist" from "a file the
        # project used has gone missing". Same state internally, completely
        # different thing to tell somebody.
        if not Path(self.path).exists():
            raise MediaOfflineError(f"file not found: {self.path}")
        raise MediaOfflineError(
            f"media '{self.name or self.media_id}' could not be read: {self.path} "
            f"(unsupported format, or the file is damaged)")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "media_id": self.media_id, "path": self.path, "name": self.name,
            "tags": list(self.tags),
            "info": self.info.to_dict() if self.info else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaRef":
        info = data.get("info")
        return cls(media_id=data["media_id"], path=data["path"],
                   name=data.get("name", ""), tags=list(data.get("tags", [])),
                   info=MediaInfo.from_dict(info) if info else None)


class MediaRegistry:
    """Owns every media reference in a project, plus the probe cache."""

    def __init__(self, cache_path: str | Path | None = None, ffprobe: str = FFPROBE):
        self._refs: Dict[str, MediaRef] = {}
        self._by_path: Dict[str, str] = {}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_path = Path(cache_path) if cache_path else None
        self._ffprobe = ffprobe
        self._load_cache()

    # -- cache -----------------------------------------------------------

    def _cache_key(self, path: Path) -> str:
        try:
            stat = path.stat()
            return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        except OSError:
            return f"{path}|missing"

    def _load_cache(self) -> None:
        if self._cache_path and self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._cache = {}  # a corrupt cache is a re-probe, not a failure

    def save_cache(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")
        tmp.replace(self._cache_path)  # atomic: a crash never leaves half a cache

    # -- registration ----------------------------------------------------

    def add(self, path: str | Path, name: str = "", media_id: str | None = None,
            allow_offline: bool = True) -> MediaRef:
        """Register a file. Returns the existing ref if the path is known."""
        path = Path(path)
        resolved = str(path.resolve()) if path.exists() else str(path)
        if resolved in self._by_path:
            return self._refs[self._by_path[resolved]]

        info: Optional[MediaInfo] = None
        try:
            info = self._probe_cached(path)
        except MediaOfflineError:
            if not allow_offline:
                raise

        ref = MediaRef(
            media_id=media_id or f"m-{uuid.uuid4().hex[:12]}",
            path=resolved,
            info=info,
            name=name or path.name,
        )
        self._refs[ref.media_id] = ref
        self._by_path[resolved] = ref.media_id
        return ref

    def _probe_cached(self, path: Path) -> MediaInfo:
        key = self._cache_key(path)
        cached = self._cache.get(key)
        if cached is not None:
            return MediaInfo.from_dict(cached)
        info = probe(path, self._ffprobe)
        self._cache[key] = info.to_dict()
        return info

    def add_ref(self, ref: MediaRef) -> MediaRef:
        """Adopt a pre-built ref (generated media, colour mattes, test doubles).

        Bypasses probing, so the caller is responsible for the MediaInfo being
        truthful about the file.
        """
        self._refs[ref.media_id] = ref
        self._by_path[ref.path] = ref.media_id
        return ref

    def get(self, media_id: str) -> MediaRef:
        try:
            return self._refs[media_id]
        except KeyError:
            raise MediaError(f"unknown media id {media_id!r}") from None

    def __contains__(self, media_id: object) -> bool:
        return media_id in self._refs

    def __len__(self) -> int:
        return len(self._refs)

    def __iter__(self) -> Iterable[MediaRef]:
        return iter(self._refs.values())

    @property
    def offline(self) -> List[MediaRef]:
        return [r for r in self._refs.values() if not r.online]

    def relink(self, media_id: str, new_path: str | Path) -> MediaRef:
        """Point an existing media id at a new file. Every clip follows."""
        ref = self.get(media_id)
        new_path = Path(new_path)
        info = self._probe_cached(new_path)  # raises if the new path is bad too
        self._by_path.pop(ref.path, None)
        ref.path = str(new_path.resolve())
        ref.info = info
        self._by_path[ref.path] = media_id
        return ref

    def relink_directory(self, directory: str | Path) -> List[MediaRef]:
        """Relink every offline ref whose filename is found under `directory`.

        The usual real-world case: the whole media folder moved.
        """
        directory = Path(directory)
        by_name: Dict[str, Path] = {}
        if directory.exists():
            for candidate in directory.rglob("*"):
                if candidate.is_file():
                    by_name.setdefault(candidate.name, candidate)
        healed: List[MediaRef] = []
        for ref in self.offline:
            match = by_name.get(Path(ref.path).name)
            if match is not None:
                healed.append(self.relink(ref.media_id, match))
        return healed

    def refresh(self, media_id: str) -> MediaRef:
        """Re-probe (after a file was replaced in place)."""
        ref = self.get(media_id)
        try:
            ref.info = self._probe_cached(Path(ref.path))
        except MediaOfflineError:
            ref.info = None
        return ref

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {"media": [ref.to_dict() for ref in self._refs.values()]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any], cache_path: str | Path | None = None,
                  reprobe_missing: bool = True) -> "MediaRegistry":
        registry = cls(cache_path=cache_path)
        for item in data.get("media", []):
            ref = MediaRef.from_dict(item)
            # Trust the stored info only if the file still looks identical;
            # otherwise re-probe, or mark offline.
            path = Path(ref.path)
            if reprobe_missing:
                if not path.exists():
                    ref.info = None
                elif ref.info is not None:
                    try:
                        stat = path.stat()
                        if (stat.st_size, stat.st_mtime_ns) != (ref.info.size_bytes, ref.info.mtime_ns):
                            ref.info = registry._probe_cached(path)
                    except (OSError, MediaError):
                        ref.info = None
            registry._refs[ref.media_id] = ref
            registry._by_path[ref.path] = ref.media_id
        return registry

"""
cache.py -- disk cache for analysis results.

Analysis is expensive in two different currencies: motion analysis costs
seconds of CPU per clip, and vision analysis costs money per clip. Neither may
be paid twice for the same file. Entries are keyed by file identity (path,
size, mtime) plus an analyser version, so replacing a file re-analyses it and
changing an analyser invalidates only its own entries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class AnalysisCache:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._data: Dict[str, Any] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}   # a corrupt cache costs time, never correctness

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)
        self._dirty = False

    @staticmethod
    def key(file_path: str | Path, analyser: str, version: int = 1, **extra: Any) -> str:
        file_path = Path(file_path)
        try:
            stat = file_path.stat()
            identity = f"{file_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        except OSError:
            identity = f"{file_path}|missing"
        suffix = "|".join(f"{k}={v}" for k, v in sorted(extra.items()))
        return f"{analyser}:v{version}|{identity}" + (f"|{suffix}" if suffix else "")

    def get(self, key: str) -> Optional[Any]:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> Any:
        self._data[key] = value
        self._dirty = True
        return value

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        hit = self.get(key)
        if hit is not None:
            return hit
        return self.put(key, compute())

    def __len__(self) -> int:
        return len(self._data)

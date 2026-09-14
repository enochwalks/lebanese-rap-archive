"""Interactive channel picker.

Editing YAML by hand is the step most likely to go wrong for someone who does not
write code: one wrong space and the file silently stops parsing. This numbers the
chats you are in, takes the numbers you want, and rewrites only the sources block
- every comment and setting in config.yaml survives untouched.
"""

import re
import shutil
from pathlib import Path

from . import config

SOURCES_RE = re.compile(r"^sources:.*?(?=^[a-zA-Z_]|\Z)", re.M | re.S)

SIGNAL_HINTS = ("signal", "trade", "trading", "forex", "gold", "fx", "crypto",
                "pips", "vip", "scalp", "analysis", "market", "xau")


def looks_like_signals(name):
    lowered = (name or "").lower()
    return any(hint in lowered for hint in SIGNAL_HINTS)


def render_sources(entries):
    """The YAML block for the chosen channels."""
    lines = ["sources:"]
    for entry in entries:
        safe_name = entry["name"].replace('"', "'")
        lines.append(f"  - id: {entry['id']}")
        lines.append(f'    name: "{safe_name}"')
        lines.append(f"    weight: {entry.get('weight', 1.0)}")
    return "\n".join(lines) + "\n\n"


def write_sources(entries, path=None):
    """Replace the sources block in config.yaml, preserving everything else."""
    cfg_path = Path(path or config.ROOT / "config.yaml")
    if not cfg_path.exists():
        example = config.ROOT / "config.example.yaml"
        shutil.copy(example, cfg_path)

    text = cfg_path.read_text(encoding="utf-8")
    block = render_sources(entries)
    if SOURCES_RE.search(text):
        text = SOURCES_RE.sub(block, text, count=1)
    else:
        text = block + text

    backup = cfg_path.with_suffix(".yaml.bak")
    shutil.copy(cfg_path, backup)
    cfg_path.write_text(text, encoding="utf-8")
    return cfg_path, backup


def parse_selection(raw, count):
    """'1,3,7-9' or 'all' -> a list of indexes. Unknown tokens are ignored."""
    raw = (raw or "").strip().lower()
    if raw in ("all", "*"):
        return list(range(count))
    chosen = []
    for token in re.split(r"[,\s]+", raw):
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            if start.isdigit() and end.isdigit():
                chosen.extend(range(int(start) - 1, int(end)))
        elif token.isdigit():
            chosen.append(int(token) - 1)
    return [i for i in dict.fromkeys(chosen) if 0 <= i < count]

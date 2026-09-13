"""Config loading: config.yaml + .env, with defaults that work out of the box."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "sources": [],
    "destination": "me",
    "gate": {
        "min_score": 70,
        "require_stop_loss": True,
        "min_risk_reward": 1.2,
        "max_leverage": 20,
        "max_age_minutes": 20,
        "daily_cap": 8,
    },
    "consensus": {"window_minutes": 45, "near_duplicate_ratio": 0.82},
    "scoring": {
        "weights": {
            "channel_trust": 30,
            "completeness": 15,
            "risk_reward": 15,
            "consensus": 20,
            "discipline": 10,
            "freshness": 10,
        },
        "trust_prior_trades": 10,
    },
    "outcomes": {"enabled": True, "horizon_hours": 24, "poll_minutes": 30},
    "llm_fallback": {"enabled": False, "model": "claude-sonnet-5"},
}


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_env(path=None):
    """Minimal .env reader so we don't need python-dotenv."""
    env_path = Path(path or ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load(path=None):
    load_env()
    cfg_path = Path(path or os.environ.get("SIGFILTER_CONFIG") or ROOT / "config.yaml")
    raw = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg = _merge(DEFAULTS, raw)
    cfg["_path"] = str(cfg_path)
    return cfg


def db_path():
    return os.environ.get("SIGFILTER_DB") or str(ROOT / "signals.db")


def source_map(cfg):
    """channel_id -> {name, weight}"""
    out = {}
    for src in cfg.get("sources") or []:
        out[int(src["id"])] = {
            "name": src.get("name") or str(src["id"]),
            "weight": float(src.get("weight", 1.0)),
        }
    return out

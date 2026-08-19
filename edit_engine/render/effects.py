"""
effects.py -- the clip effect registry for the ffmpeg backend.

An effect is stored on a clip as `Effect(kind, params)` -- pure data, so it
serializes, undoes and survives a round-trip through the project file whether
or not the current backend knows how to draw it. This module is the ffmpeg
backend's opinion about what those kinds mean.

Registering a new effect is one function:

    @register("vignette")
    def _vignette(params, ctx):
        return [f"vignette=PI/{params.get('angle', 4.5)}"]

Effects run *after* the clip has been conformed to the sequence raster, so
they all work in output-pixel space and compose predictably. Unknown kinds are
reported, never silently dropped -- a render that quietly ignores half the
grade is worse than one that refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, List, Tuple


@dataclass
class EffectContext:
    """What an effect compiler is allowed to know about its clip."""

    width: int
    height: int
    frame_rate: Fraction
    duration: Fraction          # seconds on the timeline

    @property
    def frames(self) -> int:
        return max(1, int(self.duration * self.frame_rate))


EffectCompiler = Callable[[Dict, EffectContext], List[str]]

REGISTRY: Dict[str, EffectCompiler] = {}


def register(kind: str) -> Callable[[EffectCompiler], EffectCompiler]:
    def decorator(func: EffectCompiler) -> EffectCompiler:
        REGISTRY[kind] = func
        return func
    return decorator


def _number(params: Dict, key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


@register("zoom")
def _zoom(params: Dict, ctx: EffectContext) -> List[str]:
    """Static punch-in. `factor` 1.0 = untouched."""
    factor = max(1.0, _number(params, "factor", 1.1))
    if factor == 1.0:
        return []
    return [f"scale=iw*{factor:.4f}:ih*{factor:.4f}",
            f"crop={ctx.width}:{ctx.height}"]


@register("punch")
def _punch(params: Dict, ctx: EffectContext) -> List[str]:
    """The beat hit: snap in, settle back over `settle` seconds."""
    amount = _number(params, "amount", 0.12)
    settle = max(1, int(_number(params, "settle", 0.4) * float(ctx.frame_rate)))
    zoom = f"if(lt(on,{settle}),{1 + amount:.4f}-{amount:.4f}*(on/{settle}),1.0)"
    return [f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={ctx.width}x{ctx.height}:fps={ctx.frame_rate}"]


@register("ken_burns")
def _ken_burns(params: Dict, ctx: EffectContext) -> List[str]:
    """Slow push or drift across a still. `motion`: zoom_in|zoom_out|pan_lr|pan_rl|diag."""
    motion = str(params.get("motion", "zoom_in"))
    amount = _number(params, "amount", 0.15)
    frames = ctx.frames
    if motion == "zoom_out":
        zoom = f"{1 + amount:.4f}-{amount:.4f}*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "pan_lr":
        zoom = f"{1 + amount:.4f}"
        x, y = f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    elif motion == "pan_rl":
        zoom = f"{1 + amount:.4f}"
        x, y = f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"
    elif motion == "diag":
        zoom = f"1.05+{amount:.4f}*on/{frames}"
        x, y = f"(iw-iw/zoom)*on/{frames}", f"(ih-ih/zoom)*on/{frames}"
    else:
        zoom = f"1.0+{amount:.4f}*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    # Oversample first: zoompan steps in whole source pixels, and without the
    # headroom a slow move judders visibly.
    return [f"scale={ctx.width * 3}:-2",
            f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:"
            f"s={ctx.width}x{ctx.height}:fps={ctx.frame_rate}"]


@register("fade")
def _fade(params: Dict, ctx: EffectContext) -> List[str]:
    """Fade from/to a colour at the clip's own edges."""
    filters: List[str] = []
    color = str(params.get("color", "black"))
    fade_in = _number(params, "in", 0.0)
    fade_out = _number(params, "out", 0.0)
    if fade_in > 0:
        filters.append(f"fade=t=in:st=0:d={fade_in:.4f}:color={color}")
    if fade_out > 0:
        start = max(0.0, float(ctx.duration) - fade_out)
        filters.append(f"fade=t=out:st={start:.4f}:d={fade_out:.4f}:color={color}")
    return filters


@register("flash")
def _flash(params: Dict, ctx: EffectContext) -> List[str]:
    """White frame on the cut -- a hard accent, kept short on purpose."""
    duration = _number(params, "duration", 0.1)
    return [f"fade=t=in:st=0:d={duration:.4f}:color=white"]


@register("color")
def _color(params: Dict, ctx: EffectContext) -> List[str]:
    """Primary correction: brightness/contrast/saturation/gamma."""
    parts = []
    for key, default in (("brightness", 0.0), ("contrast", 1.0),
                         ("saturation", 1.0), ("gamma", 1.0)):
        value = _number(params, key, default)
        if value != default:
            parts.append(f"{key}={value:.4f}")
    return [f"eq={':'.join(parts)}"] if parts else []


@register("vignette")
def _vignette(params: Dict, ctx: EffectContext) -> List[str]:
    angle = _number(params, "angle", 4.5)
    return [f"vignette=PI/{angle:.3f}"]


@register("blur")
def _blur(params: Dict, ctx: EffectContext) -> List[str]:
    sigma = _number(params, "sigma", 5.0)
    return [f"gblur=sigma={sigma:.3f}"]


def compile_effects(effects: List[Dict], ctx: EffectContext) -> Tuple[List[str], List[str]]:
    """Compile a clip's effect stack in order. Returns (filters, warnings)."""
    filters: List[str] = []
    warnings: List[str] = []
    for effect in effects:
        kind = effect.get("kind", "")
        compiler = REGISTRY.get(kind)
        if compiler is None:
            warnings.append(f"unknown effect {kind!r} was not rendered")
            continue
        filters.extend(compiler(effect.get("params", {}) or {}, ctx))
    return filters, warnings

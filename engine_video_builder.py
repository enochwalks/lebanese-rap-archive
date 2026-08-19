"""
engine_video_builder.py
Drop-in alternative to video_builder.build_video, backed by edit_engine.

WHY BOTH EXIST
--------------
`video_builder.py` renders by spawning one ffmpeg process per shot, writing
each to a temp file, concatenating them and muxing the song on top. It works,
and it is what the channel ships today, so it is untouched.

This module does the same job by building a real timeline and rendering it in
a single pass. Concretely, for a 3-minute song at ~2.5s shots:

    video_builder     ~70 ffmpeg processes, ~70 temp files, 3 passes
    engine            1 ffmpeg process, no temp files, 1 pass

and the edit itself becomes inspectable: you get a Sequence you can print,
export as an EDL for Resolve/Premiere, tweak, and re-render.

HONEST LIMITATION
-----------------
The "visualizer" and "combo" styles are not supported here yet. Those draw an
audio-reactive waveform, EQ bars and title text across the whole programme --
sequence-level generated layers, which the engine gets when generator tracks
land in a later phase. Ask for them and this module tells you to use
video_builder rather than quietly rendering something that looks different.

USAGE (opt-in, nothing switches on its own)
-------------------------------------------
In main.py, replace

    import video_builder
    ...
    video_builder.build_video(...)

with

    import engine_video_builder as video_builder

and set VIDEO_STYLE to "clips" or "kenburns".
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import List, Optional

from edit_engine import Project, Sequence
from edit_engine.edl import to_edl
from edit_engine.programs.beat_edit import BeatEditStyle, build_music_video, collect_visuals
from edit_engine.render import FFmpegRenderer, RenderSettings, compile_plan

BASE_DIR = Path(__file__).parent
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = Fraction(30)

UNSUPPORTED_STYLES = {
    "visualizer": "the audio-reactive visualizer",
    "combo": "the combo (imagery + visualizer overlay) look",
}


def _visual_sources(style: str, anime_clip_path: str = "") -> List[Path]:
    if style == "kenburns":
        sources = collect_visuals(BASE_DIR / "artwork")
        if not sources:
            raise FileNotFoundError(
                f"No images in {BASE_DIR / 'artwork'} for the kenburns style.")
        return sources

    sources = collect_visuals(BASE_DIR / "anime_clips")
    if not sources and anime_clip_path:
        sources = [Path(anime_clip_path)]
    if not sources:
        raise FileNotFoundError(
            f"No clips in {BASE_DIR / 'anime_clips'} for the clips style.")
    return sources


def build_video(song_path, anime_clip_path, output_path, artist, title, release_date,
                logo_path=None, style: str = "clips", seed: Optional[int] = None,
                save_project: bool = True, export_edl: bool = True,
                progress: bool = True):
    """Build and render one video. Signature matches video_builder.build_video.

    Also writes `<output>.json` (the editable project) and `<output>.edl`
    (hand-off to a real NLE) next to the video unless asked not to.
    """
    if style in UNSUPPORTED_STYLES:
        raise NotImplementedError(
            f"engine_video_builder does not render {UNSUPPORTED_STYLES[style]} yet "
            f"(generated overlay layers are a later phase). Use video_builder.py "
            f"for style={style!r}, or pick 'clips' / 'kenburns' here.")

    output_path = Path(output_path)
    project = Project(name=f"{artist} - {title}")
    sequence = project.add_sequence(Sequence.create(
        name=f"{artist} - {title}", rate=FPS, width=VIDEO_WIDTH, height=VIDEO_HEIGHT))

    intro = BASE_DIR / "intro_video.mp4"
    sequence, stack = build_music_video(
        project, song_path, _visual_sources(style, str(anime_clip_path or "")),
        sequence=sequence, style=BeatEditStyle(),
        intro_path=intro if intro.exists() else None,
        seed=seed, name=f"{artist} - {title}",
    )

    problems = sequence.validate(project.registry)
    if problems:
        raise RuntimeError("timeline failed validation:\n  " + "\n  ".join(problems))

    plan = compile_plan(sequence, project.registry,
                        settings=RenderSettings(preset="medium", crf=19))
    print(f"[engine] {len(sequence.video_tracks[0].clips)} shots, "
          f"{plan.frame_count} frames, {len(plan.inputs)} sources")
    for warning in plan.warnings:
        print(f"[engine] warning: {warning}")

    def report(done: float, total: float) -> None:
        if total > 0:
            print(f"\r[engine] rendering: {min(100, int(done / total * 100))}%",
                  end="", flush=True)

    FFmpegRenderer().render(plan, output_path, progress=report if progress else None)
    if progress:
        print()

    if save_project:
        project.save(output_path.with_suffix(".json"))
    if export_edl:
        export = to_edl(sequence, project.registry, title=f"{artist} - {title}")
        export.write(output_path.with_suffix(".edl"))
        for warning in export.warnings:
            print(f"[engine] EDL: {warning}")

    print(f"[engine] Done -> {output_path} (style={style})")
    return str(output_path)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if len(args) < 4:
        print("Usage: python engine_video_builder.py <song> <output.mp4> "
              "<artist> <title> [style: clips|kenburns] [seed]")
        sys.exit(1)
    song, out, artist_arg, title_arg = args[:4]
    style_arg = args[4] if len(args) > 4 else "clips"
    seed_arg = int(args[5]) if len(args) > 5 else None
    build_video(song, "", out, artist_arg, title_arg, "", style=style_arg, seed=seed_arg)

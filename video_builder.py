"""
video_builder.py
Builds the final video for one song:
  1. INTRO segment: the pre-rendered animated intro (intro_video.mp4),
     re-encoded to match the main segment's size/fps. Silent (no audio yet).
  2. MAIN segment, one of three styles:
       "clips"      - beat-synced edit rotating through all anime clips (default)
       "visualizer" - audio-reactive waveform + spectrum, generated from the song
       "kenburns"   - static artwork from artwork/ with slow zoom/pan motion,
                      switching images on the beat
  3. The two segments are concatenated into one final video.
"""

import subprocess
import json
import tempfile
import os
import re
import random
from pathlib import Path

INTRO_DURATION = 4
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30

MIN_BEATS_PER_CUT = 4
MAX_BEATS_PER_CUT = 8
MIN_CUT_SECONDS = 1.5   # hard floor: never hold a shot shorter than this
FALLBACK_CUT_SECONDS = 2.5  # cut interval when beat detection is unavailable

# visualizer colors (Lebanese flag red + green)
VIS_WAVE_COLORS = "0xEE1C25|0x00A651"
VIS_BACKGROUND_A = "0x0a0a10"   # gradient top color
VIS_BACKGROUND_B = "0x241016"   # gradient bottom color (warm dark red)
VIS_PROGRESS_COLOR = "0xEE1C25@0.85"
CHANNEL_NAME = "L E B A N E S E   R A P   A R C H I V E"


def get_duration(file_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(file_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def get_audio_end(file_path):
    full_duration = get_duration(file_path)
    cmd = [
        "ffmpeg", "-i", str(file_path),
        "-af", "silencedetect=noise=-50dB:d=0.5",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    starts = [float(s) for s in re.findall(r"silence_start:\s*(-?[\d.]+)", result.stderr)]
    ends = [float(e) for e in re.findall(r"silence_end:\s*(-?[\d.]+)", result.stderr)]
    if not starts:
        return full_duration
    last_start = starts[-1]
    reaches_eof = (len(starts) > len(ends)) or (ends and ends[-1] >= full_duration - 1.0)
    if reaches_eof:
        return last_start
    return full_duration


def run_ffmpeg_with_progress(cmd, expected_duration, stage_label):
    import threading

    cmd_with_progress = [cmd[0], "-progress", "pipe:1"] + cmd[1:]
    print(f"[video_builder] {stage_label}: starting (expected ~{expected_duration:.1f}s of output)...")

    process = subprocess.Popen(
        cmd_with_progress,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stderr_lines = []

    def drain_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    last_percent_shown = -1
    out_time_pattern = re.compile(r"out_time_ms=(\d+)")

    for line in process.stdout:
        match = out_time_pattern.search(line)
        if match and expected_duration > 0:
            out_time_seconds = int(match.group(1)) / 1_000_000
            percent = min(100, int((out_time_seconds / expected_duration) * 100))
            if percent != last_percent_shown:
                print(f"\r[video_builder] {stage_label}: {percent}%", end="", flush=True)
                last_percent_shown = percent
        if "progress=end" in line:
            print(f"\r[video_builder] {stage_label}: 100%", flush=True)

    process.wait()
    stderr_thread.join(timeout=5)
    print()
    return process.returncode, "".join(stderr_lines)


def _find_font():
    """Find a usable bold font across Windows / Linux / macOS. Returns None if none found."""
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def build_intro_segment(logo_path, output_path, duration=INTRO_DURATION):
    intro_video = Path(__file__).parent / "intro_video.mp4"
    if not intro_video.exists():
        raise FileNotFoundError(
            f"Animated intro not found at {intro_video}. "
            f"Copy your lebanese_rap_intro.mp4 there as intro_video.mp4."
        )

    output_path = str(output_path)
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(intro_video),
        "-an",
        "-vf", vf,
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    print(f"[video_builder] Building intro from animated video: {intro_video.name}")
    returncode, stderr_text = run_ffmpeg_with_progress(cmd, duration, "Intro (animated)")
    if returncode != 0:
        print("[video_builder] FFMPEG STDERR (intro):\n", stderr_text[-3000:])
        raise RuntimeError("ffmpeg failed building the intro from the animated video")


def detect_beat_cuts(song_path, min_beats=MIN_BEATS_PER_CUT,
                     max_beats=MAX_BEATS_PER_CUT, max_time=None):
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(str(song_path))
        _tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = [float(t) for t in librosa.frames_to_time(beats, sr=sr)]
    except Exception as e:
        print(f"[video_builder] beat detection unavailable ({e}); using fixed {FALLBACK_CUT_SECONDS}s cuts")
        beat_times = []

    if max_time is None:
        max_time = beat_times[-1] if beat_times else 0.0
    end = float(max_time)

    # Guard: a near-zero duration would produce zero pieces and crash the
    # concat step downstream. Emit one minimal piece instead.
    if end <= 0.5:
        return [0.0, max(end, 0.5)]

    cuts = [0.0]
    if beat_times:
        i = 0
        while i < len(beat_times):
            i += random.randint(min_beats, max_beats)
            if i >= len(beat_times) or beat_times[i] >= end:
                break
            # honor the minimum shot length: if this beat lands too soon
            # after the previous cut, keep walking beats until it doesn't
            while i < len(beat_times) and beat_times[i] - cuts[-1] < MIN_CUT_SECONDS:
                i += 1
            if i < len(beat_times) and beat_times[i] < end:
                cuts.append(beat_times[i])
            else:
                break
    else:
        t = FALLBACK_CUT_SECONDS
        while t < end:
            cuts.append(t)
            t += FALLBACK_CUT_SECONDS

    cuts.append(end)

    # merge any cut that would create a shot shorter than MIN_CUT_SECONDS
    clean = [cuts[0]]
    for c in cuts[1:]:
        if c - clean[-1] >= MIN_CUT_SECONDS:
            clean.append(c)
    if clean[-1] < end:
        clean[-1] = end
    return clean


def build_beat_piece(clip_path, clip_start, duration, out_path, effect="punch"):
    """
    effect:
      "punch"     - quick zoom-in that settles (the classic beat hit)
      "slow_zoom" - gradual push-in across the whole piece (good for long cuts)
      "flash"     - white flash on the cut + zoom punch (hard beat accent)
      "plain"     - clean cut, no movement (breathing room)
    """
    base_scale = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
    )
    zoom_center = f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"

    if effect in ("punch", "flash"):
        punch_frames = max(1, int(0.4 * FPS))
        zoom_expr = f"if(lt(on,{punch_frames}),1.12-0.12*(on/{punch_frames}),1.0)"
        vf = (
            f"{base_scale},"
            f"zoompan=z='{zoom_expr}':{zoom_center}:"
            f"d=1:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
        )
        if effect == "flash":
            vf += ",fade=t=in:st=0:d=0.1:color=white"
    elif effect == "slow_zoom":
        total_frames = max(1, int(duration * FPS))
        zoom_expr = f"min(1.0+0.10*(on/{total_frames}),1.12)"
        vf = (
            f"{base_scale},"
            f"zoompan=z='{zoom_expr}':{zoom_center}:"
            f"d=1:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
        )
    else:  # plain
        vf = base_scale

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-ss", str(clip_start),
        "-i", str(clip_path),
        "-t", str(duration),
        "-an",
        "-vf", vf,
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("[video_builder] FFMPEG STDERR (beat piece):\n", result.stderr[-2000:])
        raise RuntimeError(f"ffmpeg failed building beat piece at clip_start={clip_start}")


def choose_effect(seg_duration, piece_index, rng=random):
    """
    Picks a cut style so the edit isn't the same punch on every beat:
      - long segments (>3.5s) lean toward a slow push-in
      - roughly 1 in 6 cuts lands a white flash accent
      - the rest mix zoom punches with clean cuts
    """
    if seg_duration > 3.5:
        return rng.choices(["slow_zoom", "punch", "plain"], weights=[6, 2, 2])[0]
    if piece_index > 0 and rng.random() < 0.16:
        return "flash"
    return rng.choices(["punch", "plain", "slow_zoom"], weights=[5, 3, 2])[0]


# ---------------------------------------------------------------------------
# Ken Burns (static artwork with motion)
# ---------------------------------------------------------------------------

KENBURNS_MOTIONS = ["zoom_in", "zoom_out", "pan_lr", "pan_rl", "diag"]


def build_kenburns_piece(image_path, duration, out_path, motion="zoom_in"):
    """One artwork shot with slow cinematic motion (zoom or pan)."""
    frames = max(1, int(duration * FPS))
    if motion == "zoom_in":
        z = f"1.0+0.15*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        z = f"1.15-0.15*on/{frames}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "pan_lr":
        z = "1.15"
        x, y = f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    elif motion == "pan_rl":
        z = "1.15"
        x, y = f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"
    else:  # diag
        z = f"1.05+0.10*on/{frames}"
        x, y = f"(iw-iw/zoom)*on/{frames}", f"(ih-ih/zoom)*on/{frames}"

    # pre-scale large so zoompan doesn't jitter on subpixel steps
    vf = (
        f"scale=3200:-2,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS},"
        f"format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(image_path),
        "-t", str(duration),
        "-vf", vf,
        "-frames:v", str(frames),
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("[video_builder] FFMPEG STDERR (kenburns piece):\n", result.stderr[-2000:])
        raise RuntimeError(f"ffmpeg failed building ken burns piece from {image_path}")


def _concat_pieces_and_mux(work_dir, piece_paths, song_path, output_path,
                           main_duration, stage_label):
    """Concat rendered pieces (stream copy) and mux the song audio on top."""
    list_path = work_dir / "_pieces_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in piece_paths:
            # absolute paths: ffmpeg resolves concat entries relative to the
            # list file, which breaks when output_path is a relative path
            f.write(f"file '{Path(p).resolve().as_posix()}'\n")

    video_only = work_dir / "_video_only.mp4"
    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(video_only)
    ]
    r = subprocess.run(cmd_concat, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("[video_builder] FFMPEG STDERR (piece concat):\n", r.stderr[-2000:])
        raise RuntimeError("ffmpeg failed concatenating pieces")

    cmd_mux = [
        "ffmpeg", "-y",
        "-i", str(video_only),
        "-i", str(song_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(main_duration),
        str(output_path)
    ]
    print(f"[video_builder] Muxing song audio over {stage_label} video...")
    returncode, stderr_text = run_ffmpeg_with_progress(cmd_mux, main_duration, f"Main video ({stage_label})")
    if returncode != 0:
        print("[video_builder] FFMPEG STDERR (mux):\n", stderr_text[-3000:])
        raise RuntimeError("ffmpeg failed muxing audio onto video")


def _cleanup_work_dir(work_dir, piece_paths):
    for p in piece_paths:
        try:
            os.remove(p)
        except OSError:
            pass
    for extra in ("_pieces_list.txt", "_video_only.mp4"):
        try:
            os.remove(work_dir / extra)
        except OSError:
            pass
    try:
        work_dir.rmdir()
    except OSError:
        pass


def build_kenburns_segment(song_path, image_paths, output_path, main_duration,
                           mux_audio=True):
    """Beat-synced slideshow: artwork switches on the beat, each shot has slow motion."""
    image_paths = [str(p) for p in image_paths]
    song_path = str(song_path)

    cuts = detect_beat_cuts(song_path, max_time=main_duration)
    n_pieces = len(cuts) - 1
    print(f"[video_builder] {n_pieces} beat-synced artwork shots planned over {main_duration:.1f}s "
          f"using {len(image_paths)} image(s)")

    work_dir = Path(output_path).parent / "_beat_pieces_tmp"
    work_dir.mkdir(exist_ok=True)
    piece_paths = []

    try:
        prev_img_i = -1
        prev_motion = None
        for idx in range(n_pieces):
            seg_dur = cuts[idx + 1] - cuts[idx]

            if len(image_paths) == 1:
                img_i = 0
            else:
                choices = [i for i in range(len(image_paths)) if i != prev_img_i]
                img_i = random.choice(choices)
            prev_img_i = img_i

            motions = [m for m in KENBURNS_MOTIONS if m != prev_motion]
            motion = random.choice(motions)
            prev_motion = motion

            piece = work_dir / f"piece_{idx:04d}.mp4"
            build_kenburns_piece(image_paths[img_i], seg_dur, piece, motion=motion)
            piece_paths.append(piece)
            if idx % 20 == 0 or idx == n_pieces - 1:
                print(f"[video_builder] built shot {idx + 1}/{n_pieces}")

        if mux_audio:
            _concat_pieces_and_mux(work_dir, piece_paths, song_path, output_path,
                                   main_duration, "ken burns")
        else:
            # video-only concat (used by the combo style, which muxes later)
            list_path = work_dir / "_pieces_list.txt"
            with open(list_path, "w", encoding="utf-8") as f:
                for p in piece_paths:
                    f.write(f"file '{Path(p).resolve().as_posix()}'\n")
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(list_path), "-c", "copy", str(output_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                print("[video_builder] FFMPEG STDERR (kb concat):\n", r.stderr[-2000:])
                raise RuntimeError("ffmpeg failed concatenating ken burns pieces")
    finally:
        _cleanup_work_dir(work_dir, piece_paths)


# ---------------------------------------------------------------------------
# Audio-reactive visualizer
# ---------------------------------------------------------------------------

def _drawtext_safe(text):
    """Escape a string for use inside an ffmpeg drawtext filter."""
    return (text.replace("\\", "")
                .replace("'", "")
                .replace(":", r"\:")
                .replace(",", r"\,")
                .replace("%", r"\%"))


def build_visualizer_segment(song_path, output_path, main_duration, artist="", title=""):
    """
    Generates the whole main segment from the song itself:
      - slowly shifting dark gradient background (black -> deep warm red)
      - faint scrolling spectrum for texture
      - glowing red/green waveform (cline) reacting to the audio
      - channel name at the top, artist (large) + title (smaller) at the bottom
      - thin red progress bar tracking the song
      - soft vignette for depth
    No source footage needed, zero copyright risk.
    """
    song_path = str(song_path)
    output_path = str(output_path)

    # render 5% oversized, then a slowly drifting crop gives the whole scene
    # a gentle "camera float" so no frame is ever static
    pad_w = int(VIDEO_WIDTH * 1.05) // 2 * 2
    pad_h = int(VIDEO_HEIGHT * 1.05) // 2 * 2
    drift_x = max(1, (pad_w - VIDEO_WIDTH) // 2)
    drift_y = max(1, (pad_h - VIDEO_HEIGHT) // 2)

    wave_h = int(pad_h * 0.37)
    bars_h = int(pad_h * 0.30)
    font = _find_font()
    draw = ""
    if font:
        fp = Path(font).as_posix()
        parts = []
        if CHANNEL_NAME:
            parts.append(
                f"drawtext=fontfile='{fp}':text='{_drawtext_safe(CHANNEL_NAME)}':"
                f"fontcolor=white@0.5:fontsize={max(12, int(VIDEO_HEIGHT * 0.031))}:"
                f"x=(w-text_w)/2:y={int(VIDEO_HEIGHT * 0.059)}"
            )
        if artist:
            parts.append(
                f"drawtext=fontfile='{fp}':text='{_drawtext_safe(artist.upper())}':"
                f"fontcolor=white@0.92:fontsize={max(20, int(VIDEO_HEIGHT * 0.059))}:"
                f"x=(w-text_w)/2:y=h-{int(VIDEO_HEIGHT * 0.194)}"
            )
        if title:
            parts.append(
                f"drawtext=fontfile='{fp}':text='{_drawtext_safe(title.upper())}':"
                f"fontcolor=white@0.7:fontsize={max(14, int(VIDEO_HEIGHT * 0.037))}:"
                f"x=(w-text_w)/2:y=h-{int(VIDEO_HEIGHT * 0.12)}"
            )
        if parts:
            draw = "," + ",".join(parts)

    progress = (
        f",drawbox=x=0:y=ih-{max(4, int(VIDEO_HEIGHT * 0.0074))}:"
        f"w='iw*t/{max(main_duration, 0.1)}':h={max(4, int(VIDEO_HEIGHT * 0.0074))}:"
        f"color={VIS_PROGRESS_COLOR}:t=fill"
    )

    filter_complex = (
        # faint scrolling spectrum with a slow breathing hue shift
        f"[1:a]showspectrum=s={pad_w}x{pad_h}:mode=combined:color=fire:"
        f"scale=cbrt:slide=scroll,hue=h='18*sin(2*PI*t/25)',format=rgba,"
        f"colorchannelmixer=aa=0.22[spec];"
        f"[0:v][spec]overlay=0:0[bg];"
        # red EQ "skyline" bars along the bottom (mono mix, chunky pixels)
        f"[1:a]pan=mono|c0=.5*c0+.5*c1,showfreqs=s={pad_w // 4}x{bars_h // 2}:mode=bar:"
        f"ascale=cbrt:fscale=log:win_size=1024:averaging=1:colors=0xEE1C25,"
        f"scale={pad_w}:{bars_h}:flags=neighbor,format=rgba,colorchannelmixer=aa=0.9[bars];"
        f"[bg][bars]overlay=0:main_h-overlay_h-{int(pad_h * 0.019)}[b2];"
        # glowing center waveform
        f"[1:a]showwaves=s={pad_w}x{wave_h}:mode=cline:rate={FPS}:"
        f"colors={VIS_WAVE_COLORS},format=rgba[wave];"
        f"[wave]split[w1][w2];"
        f"[w1]gblur=sigma=16[glow];"
        f"[b2][glow]overlay=0:(main_h-overlay_h)/2[g1];"
        f"[g1][w2]overlay=0:(main_h-overlay_h)/2[g2];"
        # camera float, then fixed text/progress/vignette on top
        f"[g2]crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
        f"x='{drift_x}+{drift_x - 2}*sin(2*PI*t/37)':"
        f"y='{drift_y}+{max(1, drift_y - 2)}*sin(2*PI*t/53)'[drift];"
        f"[drift]null{draw}{progress},vignette=PI/4.5[vout]"
    )

    background = (
        f"gradients=s={pad_w}x{pad_h}:c0={VIS_BACKGROUND_A}:c1={VIS_BACKGROUND_B}:"
        f"x0=0:y0=0:x1={pad_w}:y1={pad_h}:speed=0.01:rate={FPS}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", background,
        "-i", song_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a",
        "-t", str(main_duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        output_path
    ]
    print("[video_builder] Rendering audio-reactive visualizer...")
    returncode, stderr_text = run_ffmpeg_with_progress(cmd, main_duration, "Main video (visualizer)")
    if returncode != 0:
        print("[video_builder] FFMPEG STDERR (visualizer):\n", stderr_text[-3000:])
        raise RuntimeError("ffmpeg failed rendering the visualizer segment")


def build_combo_segment(song_path, image_paths, output_path, main_duration,
                        artist="", title=""):
    """
    The "full experience" style (music 50 / video 50):
      - background: beat-synced Ken Burns imagery from artwork/ (real visual
        content that cuts and moves with the song)
      - foreground: glowing waveform, red EQ bars, channel/artist/title text,
        progress bar, vignette
    """
    song_path = str(song_path)
    output_path = str(output_path)

    kb_tmp = str(Path(output_path).parent / "_combo_kb_tmp.mp4")
    build_kenburns_segment(song_path, image_paths, kb_tmp, main_duration,
                           mux_audio=False)

    wave_h = int(VIDEO_HEIGHT * 0.35)
    bars_h = int(VIDEO_HEIGHT * 0.30)

    font = _find_font()
    draw = ""
    if font:
        fp = Path(font).as_posix()
        parts = []
        if CHANNEL_NAME:
            parts.append(
                f"drawtext=fontfile='{fp}':text='{_drawtext_safe(CHANNEL_NAME)}':"
                f"fontcolor=white@0.55:fontsize={max(12, int(VIDEO_HEIGHT * 0.031))}:"
                f"x=(w-text_w)/2:y={int(VIDEO_HEIGHT * 0.059)}"
            )
        if artist:
            parts.append(
                f"drawtext=fontfile='{fp}':text='{_drawtext_safe(artist.upper())}':"
                f"fontcolor=white@0.94:fontsize={max(20, int(VIDEO_HEIGHT * 0.059))}:"
                f"x=(w-text_w)/2:y=h-{int(VIDEO_HEIGHT * 0.194)}:"
                f"shadowcolor=black@0.7:shadowx=2:shadowy=2"
            )
        if title:
            parts.append(
                f"drawtext=fontfile='{fp}':text='{_drawtext_safe(title.upper())}':"
                f"fontcolor=white@0.75:fontsize={max(14, int(VIDEO_HEIGHT * 0.037))}:"
                f"x=(w-text_w)/2:y=h-{int(VIDEO_HEIGHT * 0.12)}:"
                f"shadowcolor=black@0.7:shadowx=2:shadowy=2"
            )
        if parts:
            draw = "," + ",".join(parts)

    bar_px = max(4, int(VIDEO_HEIGHT * 0.0074))
    progress = (
        f",drawbox=x=0:y=ih-{bar_px}:"
        f"w='iw*t/{max(main_duration, 0.1)}':h={bar_px}:"
        f"color={VIS_PROGRESS_COLOR}:t=fill"
    )

    filter_complex = (
        # slightly darken/desaturate the imagery so overlays read clearly
        f"[0:v]eq=brightness=-0.06:saturation=0.9[bg];"
        # glowing center waveform
        f"[1:a]showwaves=s={VIDEO_WIDTH}x{wave_h}:mode=cline:rate={FPS}:"
        f"colors={VIS_WAVE_COLORS},format=rgba[wave];"
        f"[wave]split[w1][w2];"
        f"[w1]gblur=sigma=15[glow];"
        f"[bg][glow]overlay=0:(main_h-overlay_h)/2[g1];"
        f"[g1][w2]overlay=0:(main_h-overlay_h)/2[g2];"
        # red EQ skyline
        f"[1:a]pan=mono|c0=.5*c0+.5*c1,showfreqs=s={VIDEO_WIDTH // 4}x{bars_h // 2}:mode=bar:"
        f"ascale=cbrt:fscale=log:win_size=1024:averaging=1:colors=0xEE1C25,"
        f"scale={VIDEO_WIDTH}:{bars_h}:flags=neighbor,format=rgba,"
        f"colorchannelmixer=aa=0.85[bars];"
        f"[g2][bars]overlay=0:main_h-overlay_h-{int(VIDEO_HEIGHT * 0.019)}[g3];"
        f"[g3]null{draw}{progress},vignette=PI/4.2[vout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", kb_tmp,
        "-i", song_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "1:a",
        "-t", str(main_duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        output_path
    ]
    print("[video_builder] Rendering combo overlay (imagery + visualizer)...")
    returncode, stderr_text = run_ffmpeg_with_progress(cmd, main_duration, "Main video (combo)")
    try:
        os.remove(kb_tmp)
    except OSError:
        pass
    if returncode != 0:
        print("[video_builder] FFMPEG STDERR (combo):\n", stderr_text[-3000:])
        raise RuntimeError("ffmpeg failed rendering the combo segment")


def build_main_segment(song_path, anime_clip_paths, output_path, main_duration):
    if isinstance(anime_clip_paths, (str, Path)):
        anime_clip_paths = [anime_clip_paths]
    anime_clip_paths = [str(p) for p in anime_clip_paths]
    song_path = str(song_path)
    output_path = str(output_path)

    clip_durations = [get_duration(p) for p in anime_clip_paths]
    cuts = detect_beat_cuts(song_path, max_time=main_duration)
    n_pieces = len(cuts) - 1
    print(f"[video_builder] {n_pieces} beat-synced cuts planned over {main_duration:.1f}s "
          f"using {len(anime_clip_paths)} clip(s)")

    work_dir = Path(output_path).parent / "_beat_pieces_tmp"
    work_dir.mkdir(exist_ok=True)
    piece_paths = []

    try:
        prev_clip_i = -1
        for idx in range(n_pieces):
            seg_dur = cuts[idx + 1] - cuts[idx]

            # pick a random clip, but never the same one twice in a row
            if len(anime_clip_paths) == 1:
                clip_i = 0
            else:
                choices = [i for i in range(len(anime_clip_paths)) if i != prev_clip_i]
                clip_i = random.choice(choices)
            prev_clip_i = clip_i
            clip_path = anime_clip_paths[clip_i]
            clip_len = clip_durations[clip_i]

            max_off = max(0.0, clip_len - seg_dur - 0.1)
            clip_start = random.uniform(0, max_off) if max_off > 0 else 0.0

            effect = choose_effect(seg_dur, idx)
            piece = work_dir / f"piece_{idx:04d}.mp4"
            build_beat_piece(clip_path, clip_start, seg_dur, piece, effect=effect)
            piece_paths.append(piece)
            if idx % 20 == 0 or idx == n_pieces - 1:
                print(f"[video_builder] built piece {idx + 1}/{n_pieces}")

        _concat_pieces_and_mux(work_dir, piece_paths, song_path, output_path,
                               main_duration, "beat-synced")
    finally:
        _cleanup_work_dir(work_dir, piece_paths)


def concat_segments(intro_path, main_path, output_path):
    intro_path = str(intro_path)
    main_path = str(main_path)
    output_path = str(output_path)

    intro_duration = get_duration(intro_path)

    intro_with_audio = str(Path(output_path).parent / "_intro_with_audio_tmp.mp4")
    cmd_add_silence = [
        "ffmpeg", "-y",
        "-i", intro_path,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(intro_duration),
        "-shortest",
        intro_with_audio
    ]
    print(f"[video_builder] Adding silent audio to intro...")
    result = subprocess.run(cmd_add_silence, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("[video_builder] FFMPEG STDERR (intro audio):\n", result.stderr[-3000:])
        raise RuntimeError("ffmpeg failed adding silent audio to intro")

    concat_list_path = str(Path(output_path).parent / "_concat_list_tmp.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        f.write(f"file '{Path(intro_with_audio).resolve().as_posix()}'\n")
        f.write(f"file '{Path(main_path).resolve().as_posix()}'\n")

    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_path
    ]
    print(f"[video_builder] Concatenating segments (stream copy)...")
    total_expected = intro_duration + get_duration(main_path)
    returncode, stderr_text = run_ffmpeg_with_progress(cmd_concat, total_expected, "Final assembly")

    os.remove(intro_with_audio)
    os.remove(concat_list_path)

    if returncode != 0:
        print("[video_builder] FFMPEG STDERR (concat):\n", stderr_text[-3000:])
        raise RuntimeError("ffmpeg failed concatenating intro + main segments")


def build_video(song_path, anime_clip_path, output_path, artist, title, release_date,
                logo_path=None, style="clips"):
    """
    style:
      "clips"      - beat-synced edit from anime_clips/ (original behavior)
      "visualizer" - audio-reactive visualizer generated from the song
      "kenburns"   - artwork/ images with slow zoom/pan, switching on the beat
    """
    base_dir = Path(__file__).parent

    song_path = str(song_path)
    anime_clip_path = str(anime_clip_path) if anime_clip_path else ""
    output_path = str(output_path)

    song_duration = get_audio_end(song_path)
    main_duration = max(song_duration, 1.0)

    with tempfile.TemporaryDirectory() as tmp_dir:
        intro_tmp = os.path.join(tmp_dir, "intro.mp4")
        main_tmp = os.path.join(tmp_dir, "main.mp4")

        build_intro_segment(None, intro_tmp)

        if style == "visualizer":
            build_visualizer_segment(song_path, main_tmp, main_duration, artist, title)
        elif style == "combo":
            art_dir = base_dir / "artwork"
            img_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
            images = []
            if art_dir.exists():
                images = sorted(str(p) for p in art_dir.iterdir()
                                if p.suffix.lower() in img_exts)
            if not images:
                raise FileNotFoundError(
                    f"No images found in {art_dir}. Put artwork (jpg/png) there "
                    f"to use the combo style."
                )
            build_combo_segment(song_path, images, main_tmp, main_duration, artist, title)
        elif style == "kenburns":
            art_dir = base_dir / "artwork"
            img_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
            images = []
            if art_dir.exists():
                images = sorted(str(p) for p in art_dir.iterdir()
                                if p.suffix.lower() in img_exts)
            if not images:
                raise FileNotFoundError(
                    f"No images found in {art_dir}. Put artwork (jpg/png) there "
                    f"to use the kenburns style."
                )
            build_kenburns_segment(song_path, images, main_tmp, main_duration)
        else:
            clips_dir = base_dir / "anime_clips"
            exts = {".mp4", ".mov", ".mkv", ".webm"}
            all_clips = []
            if clips_dir.exists():
                all_clips = sorted(str(p) for p in clips_dir.iterdir()
                                   if p.suffix.lower() in exts)
            if not all_clips:
                all_clips = [anime_clip_path]
            build_main_segment(song_path, all_clips, main_tmp, main_duration)

        concat_segments(intro_tmp, main_tmp, output_path)

    total_duration = INTRO_DURATION + song_duration
    print(f"[video_builder] Done -> {output_path} (style={style}, total duration ~{total_duration:.1f}s)")
    return output_path


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    style_arg = "clips"
    if args and args[-1].lower() in ("clips", "visualizer", "kenburns", "combo"):
        style_arg = args.pop().lower()
    if len(args) == 4:
        # no clip argument (visualizer/kenburns don't need one)
        song_a, out_a, artist_a, title_a = args
        clip_a = ""
    elif len(args) == 5:
        song_a, clip_a, out_a, artist_a, title_a = args
    else:
        print("Usage: python video_builder.py <song.mp3> [anime_clip.mp4] <output.mp4> "
              "<artist> <title> [style: clips|visualizer|kenburns|combo]")
        sys.exit(1)
    build_video(song_a, clip_a, out_a, artist_a, title_a, "", style=style_arg)

# ---------------------------------------------------------------------------
# end of file
# (this trailing comment is intentional padding: a file-sync quirk was
#  observed truncating the last lines of this file; if that happens again
#  only this comment is lost, not executable code)
# ---------------------------------------------------------------------------

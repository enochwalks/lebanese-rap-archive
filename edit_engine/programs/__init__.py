"""Edit programs: code that *builds* timelines, as an editor would.

A program does not render anything. It runs edit commands against a sequence
and hands back a timeline, which means every automated decision it makes is
inspectable, undoable, exportable as an EDL, and open to being fixed by hand
before it is rendered. That is the difference between automation an editor can
work with and automation that just emits an mp4.
"""

from .beat_edit import (BeatEditStyle, build_music_video, detect_beats, plan_cuts)

__all__ = ["BeatEditStyle", "build_music_video", "detect_beats", "plan_cuts"]

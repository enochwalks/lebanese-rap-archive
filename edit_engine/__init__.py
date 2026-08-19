"""
edit_engine -- a frame-accurate, non-destructive video editing engine.

    from edit_engine import Project, Sequence, CommandStack, ops, make_clip
    from edit_engine.render import compile_plan, FFmpegRenderer

    project = Project(name="Episode 12")
    media   = project.import_media("songs/track.wav")
    seq     = project.add_sequence(Sequence.create(rate=Fraction(30000, 1001)))
    stack   = CommandStack(project)

    stack.run(ops.AddClip(seq.audio_tracks[0].track_id,
                          make_clip(media, seq, seq.zero())))
    stack.run(ops.Blade(seq.seconds(12.5)))
    stack.undo()                       # exactly back to where you were

    plan = compile_plan(seq, project.registry)
    FFmpegRenderer().render(plan, "output/episode-12.mp4")

The layers, bottom up:

    timebase   exact rational time; nothing above it ever sees a float second
    media      probing, caching, offline/relink; never writes to source files
    model      project / sequence / track / clip, with enforced invariants
    commands   transactional undo/redo
    ops        the professional edit set: blade, insert, overwrite, lift,
               extract, ripple, roll, slip, slide, retime, link
    render     timeline -> render plan -> ffmpeg (or another backend)
    edl        interchange, so an automated cut can be finished by a human
"""

from .commands import CommandStack, CommandError, EditContext, FunctionCommand
from .media import MediaError, MediaOfflineError, MediaRef, MediaRegistry, probe
from .model import (AUDIO, VIDEO, Clip, Effect, Marker, ModelError, OverlapError,
                    Project, Sequence, Track, make_clip, new_id)
from .timebase import RationalTime, TimeRange, TimebaseError
from . import ops

__version__ = "0.1.0"

__all__ = [
    "AUDIO", "VIDEO",
    "Clip", "CommandError", "CommandStack", "EditContext", "Effect",
    "FunctionCommand", "Marker", "MediaError", "MediaOfflineError", "MediaRef",
    "MediaRegistry", "ModelError", "OverlapError", "Project", "RationalTime",
    "Sequence", "TimeRange", "TimebaseError", "Track", "make_clip", "new_id",
    "ops", "probe",
]

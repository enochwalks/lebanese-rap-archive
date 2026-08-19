"""
commands.py -- the transactional command engine behind undo/redo.

WHY COMMANDS AND NOT "JUST MUTATE THE MODEL"
--------------------------------------------
Undo is not a feature you can bolt on afterwards. If edits are arbitrary
mutations spread through the codebase, undo becomes a pile of special cases
and eventually corrupts a project -- the worst possible bug in an editor,
because the user loses work and cannot tell when it happened.

So: every edit is a Command object. The stack runs it, and around that run it
captures the state of exactly the tracks the command declares it touches --
before and after. Undo restores the "before" snapshot; redo restores the
"after" one. Redo never re-executes the operation, so a command that consults
anything non-deterministic (a random cut pattern, a beat detector, the clock)
still redoes to precisely the state the user saw.

Snapshots are per-track and hold clip metadata only -- no frames, no media --
so they are cheap. A command that touches one track on a 5000-clip timeline
copies one track, not the timeline.

Compound edits (a three-point edit that touches V1 and A1, or "apply 200 beat
cuts") group into a single transaction and undo as one step, because that is
how a human thinks about them.
"""

from __future__ import annotations

import copy
import time as _time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence as Seq

from .model import Clip, Marker, ModelError, Project, Sequence, Track


class CommandError(ModelError):
    """Raised when a command cannot be performed (locked track, bad range...)."""


@dataclass
class EditContext:
    """What a command is allowed to see: the project and the sequence."""

    project: Project
    sequence: Sequence

    @property
    def registry(self):
        return self.project.registry

    @property
    def rate(self):
        return self.sequence.rate


@dataclass
class _SequenceState:
    """Sequence-level scalars some commands touch (markers, in/out points)."""

    markers: List[Marker]
    in_point: Any
    out_point: Any


@dataclass
class _Snapshot:
    tracks: Dict[str, List[Clip]] = field(default_factory=dict)
    sequence_state: Optional[_SequenceState] = None


class Command(ABC):
    """One undoable edit.

    Subclasses implement `apply()` and declare which tracks they touch. They
    must not mutate anything they did not declare -- `affected_track_ids()` is
    the contract that makes undo correct, and `CommandStack` verifies it in
    debug mode.
    """

    #: set True if the command changes markers / in-out points
    touches_sequence_state: bool = False

    def __init__(self, label: str = ""):
        self.label = label or self.__class__.__name__

    @abstractmethod
    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        """Track ids this command may modify. Computed *before* it runs."""

    @abstractmethod
    def apply(self, ctx: EditContext) -> Any:
        """Perform the edit. Return anything useful to the caller."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.label!r}>"


class CompoundCommand(Command):
    """Several commands that undo and redo as one step."""

    def __init__(self, label: str, commands: Optional[List[Command]] = None):
        super().__init__(label)
        self.commands: List[Command] = list(commands or [])

    def add(self, command: Command) -> None:
        self.commands.append(command)

    @property
    def touches_sequence_state(self) -> bool:  # type: ignore[override]
        return any(c.touches_sequence_state for c in self.commands)

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        ids: List[str] = []
        for command in self.commands:
            for track_id in command.affected_track_ids(ctx):
                if track_id not in ids:
                    ids.append(track_id)
        return ids

    def apply(self, ctx: EditContext) -> List[Any]:
        return [command.apply(ctx) for command in self.commands]


class FunctionCommand(Command):
    """Escape hatch for small edits: wrap a callable as an undoable command."""

    def __init__(self, label: str, track_ids: Seq[str],
                 func: Callable[[EditContext], Any], touches_sequence_state: bool = False):
        super().__init__(label)
        self._track_ids = list(track_ids)
        self._func = func
        self.touches_sequence_state = touches_sequence_state

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return self._track_ids

    def apply(self, ctx: EditContext) -> Any:
        return self._func(ctx)


@dataclass
class HistoryEntry:
    command: Command
    before: _Snapshot
    after: _Snapshot
    result: Any = None
    timestamp: float = field(default_factory=_time.time)

    @property
    def label(self) -> str:
        return self.command.label


class CommandStack:
    """Runs commands and owns the undo/redo history for one project."""

    def __init__(self, project: Project, history_limit: int = 500,
                 verify_declarations: bool = False):
        self.project = project
        self.history_limit = history_limit
        #: when True, every run re-checks that the command only touched the
        #: tracks it declared. Cheap enough for tests, off by default.
        self.verify_declarations = verify_declarations
        self._done: List[HistoryEntry] = []
        self._undone: List[HistoryEntry] = []
        self._transaction: Optional[CompoundCommand] = None
        self._dirty = False

    # -- state -----------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    @property
    def undo_label(self) -> Optional[str]:
        return self._done[-1].label if self._done else None

    @property
    def redo_label(self) -> Optional[str]:
        return self._undone[-1].label if self._undone else None

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_saved(self) -> None:
        self._dirty = False

    def history(self) -> List[str]:
        return [entry.label for entry in self._done]

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()

    # -- running ---------------------------------------------------------

    def run(self, command: Command, sequence: Optional[Sequence] = None) -> Any:
        """Execute a command, recording it for undo.

        Inside a transaction the command is executed immediately but recorded
        as part of the enclosing compound entry, so the whole group is one
        undo step.
        """
        sequence = sequence or self.project.active_sequence
        ctx = EditContext(self.project, sequence)

        if self._transaction is not None:
            result = self._run_inside_transaction(command, ctx)
            return result

        track_ids = list(command.affected_track_ids(ctx))
        before = self._capture(sequence, track_ids, command.touches_sequence_state)

        try:
            result = command.apply(ctx)
        except Exception:
            # Any failure rolls the sequence back to exactly where it was.
            # A half-applied edit is worse than a refused one.
            self._restore(sequence, before)
            raise

        after = self._capture(sequence, track_ids, command.touches_sequence_state)

        if self.verify_declarations:
            self._verify(sequence, track_ids, command)

        self._done.append(HistoryEntry(command, before, after, result))
        self._undone.clear()
        self._trim_history()
        self._dirty = True
        return result

    def _run_inside_transaction(self, command: Command, ctx: EditContext) -> Any:
        assert self._transaction is not None
        entry = self._pending_entry
        assert entry is not None
        new_ids = [t for t in command.affected_track_ids(ctx)
                   if t not in entry.before.tracks]
        if new_ids:
            # Widen the transaction's "before" snapshot to cover tracks this
            # step reaches that earlier steps did not -- their current state
            # is still the pre-transaction state, so capturing now is correct.
            extra = self._capture(ctx.sequence, new_ids, False)
            entry.before.tracks.update(extra.tracks)
        if command.touches_sequence_state and entry.before.sequence_state is None:
            entry.before.sequence_state = self._capture_sequence_state(ctx.sequence)

        try:
            result = command.apply(ctx)
        except Exception:
            self._restore(ctx.sequence, entry.before)
            raise
        self._transaction.add(command)
        return result

    @contextmanager
    def transaction(self, label: str, sequence: Optional[Sequence] = None) -> Iterator[CompoundCommand]:
        """Group every command run inside the block into one undo step."""
        if self._transaction is not None:
            raise CommandError("nested transactions are not supported")
        sequence = sequence or self.project.active_sequence
        compound = CompoundCommand(label)
        self._transaction = compound
        self._pending_entry = HistoryEntry(compound, _Snapshot(), _Snapshot())
        try:
            yield compound
        except Exception:
            self._restore(sequence, self._pending_entry.before)
            self._transaction = None
            self._pending_entry = None
            raise
        entry = self._pending_entry
        self._transaction = None
        self._pending_entry = None

        if not compound.commands:
            return  # nothing happened: do not push an empty undo step
        entry.after = self._capture(sequence, list(entry.before.tracks.keys()),
                                    compound.touches_sequence_state)
        self._done.append(entry)
        self._undone.clear()
        self._trim_history()
        self._dirty = True

    _pending_entry: Optional[HistoryEntry] = None

    # -- undo / redo -----------------------------------------------------

    def undo(self) -> Optional[str]:
        if not self._done:
            return None
        entry = self._done.pop()
        self._restore(self._sequence_for(entry), entry.before)
        self._undone.append(entry)
        self._dirty = True
        return entry.label

    def redo(self) -> Optional[str]:
        if not self._undone:
            return None
        entry = self._undone.pop()
        self._restore(self._sequence_for(entry), entry.after)
        self._done.append(entry)
        self._dirty = True
        return entry.label

    def undo_all(self) -> int:
        count = 0
        while self.undo():
            count += 1
        return count

    def _sequence_for(self, entry: HistoryEntry) -> Sequence:
        # Snapshots are keyed by track id, which is unique across the project,
        # so find the sequence that owns them.
        for seq in self.project.sequences:
            known = {t.track_id for t in seq.tracks}
            if entry.before.tracks and set(entry.before.tracks) <= known:
                return seq
        return self.project.active_sequence

    # -- snapshots -------------------------------------------------------

    def _capture(self, sequence: Sequence, track_ids: Seq[str],
                 with_sequence_state: bool) -> _Snapshot:
        snapshot = _Snapshot()
        for track_id in track_ids:
            snapshot.tracks[track_id] = sequence.track(track_id).snapshot()
        if with_sequence_state:
            snapshot.sequence_state = self._capture_sequence_state(sequence)
        return snapshot

    @staticmethod
    def _capture_sequence_state(sequence: Sequence) -> _SequenceState:
        return _SequenceState(copy.deepcopy(sequence.markers),
                              sequence.in_point, sequence.out_point)

    @staticmethod
    def _restore(sequence: Sequence, snapshot: _Snapshot) -> None:
        for track_id, clips in snapshot.tracks.items():
            sequence.track(track_id).restore(clips)
        if snapshot.sequence_state is not None:
            sequence.markers = copy.deepcopy(snapshot.sequence_state.markers)
            sequence.in_point = snapshot.sequence_state.in_point
            sequence.out_point = snapshot.sequence_state.out_point

    def _verify(self, sequence: Sequence, declared: Seq[str], command: Command) -> None:
        """Catch a command that mutated a track it did not declare.

        Without this, such a bug is invisible until a user hits undo and half
        the edit stays behind -- so it is worth checking in tests and CI.
        """
        declared_set = set(declared)
        for track in sequence.tracks:
            if track.track_id in declared_set:
                continue
            fingerprint = [(c.clip_id, c.start.to_seconds(), c.duration.to_seconds())
                           for c in track.clips]
            previous = self._fingerprints.get(track.track_id)
            if previous is not None and previous != fingerprint:
                raise CommandError(
                    f"{command.label} modified undeclared track {track.name!r}; "
                    f"undo would be incorrect")
        self._refresh_fingerprints(sequence)

    _fingerprints: Dict[str, List[Any]] = {}

    def _refresh_fingerprints(self, sequence: Sequence) -> None:
        self._fingerprints = {
            t.track_id: [(c.clip_id, c.start.to_seconds(), c.duration.to_seconds())
                         for c in t.clips]
            for t in sequence.tracks
        }

    def _trim_history(self) -> None:
        overflow = len(self._done) - self.history_limit
        if overflow > 0:
            del self._done[:overflow]

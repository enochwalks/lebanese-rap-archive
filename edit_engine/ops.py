"""
ops.py -- professional edit operations, as undoable commands.

The trim family is the part most homegrown editors get wrong, so the exact
semantics implemented here are spelled out. Take A [0,10) butted to B [10,20):

  ripple trim  B head +2  -> B stays at 10, becomes 8 long, its in point moves
                             2 later, and everything after B slides 2 earlier.
                             Sequence gets shorter. No gap.
  normal trim  B head +2  -> B starts at 12, 8 long. A 2-frame gap opens at 10.
                             Sequence length unchanged.
  roll         at 10, +2  -> A becomes 12 long, B starts at 12 and is 8 long.
                             The cut moves; nothing else does. Total unchanged.
  slip         B, +2      -> B still occupies [10,20); the *picture inside it*
                             shifts 2 frames later. Nothing else moves.
  slide        B, +2      -> B occupies [12,22); A grows by 2, C shrinks by 2.
                             B's content is untouched.

Every one of these clamps against the media that actually exists. You cannot
trim into frames the file does not have, and instead of silently producing a
range that explodes at render time, the operation applies as much as it can
and reports back what limited it (`TrimResult.limited_by`). That is what a
real trim tool does when it hits the end of a clip's handles.

All timeline-facing deltas are in timeline time. Source consumption is
delta * |speed|, so trims stay correct on retimed clips, and reversed clips
consume their handles from the opposite end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, List, Optional, Sequence as Seq, Tuple

from .commands import Command, CommandError, EditContext
from .model import Clip, Marker, OverlapError, Track, new_id
from .timebase import RationalTime, TimeRange

HEAD = "head"
TAIL = "tail"

# what stopped a trim from applying in full
LIMIT_MEDIA = "media"          # ran out of source frames
LIMIT_NEIGHBOUR = "neighbour"  # would collide with the next/previous clip
LIMIT_LENGTH = "length"        # would leave a zero-or-negative length clip
LIMIT_TIMELINE = "timeline"    # would move before 00:00:00:00


@dataclass
class TrimResult:
    """What a trim actually did, versus what was asked for."""

    requested: RationalTime
    applied: RationalTime
    limited_by: Optional[str] = None
    clip_ids: List[str] = field(default_factory=list)

    @property
    def was_limited(self) -> bool:
        return self.limited_by is not None

    @property
    def is_noop(self) -> bool:
        return self.applied.value == 0

    def __repr__(self) -> str:
        limit = f" limited by {self.limited_by}" if self.limited_by else ""
        return (f"TrimResult(requested={self.requested.to_float_seconds():.4f}s, "
                f"applied={self.applied.to_float_seconds():.4f}s{limit})")


# ---------------------------------------------------------------------------
# handles: how much unused source sits on either side of a clip
# ---------------------------------------------------------------------------

def source_limit(ctx: EditContext, clip: Clip) -> Optional[RationalTime]:
    """Length of the media behind a clip, or None when it is unbounded
    (a still image) or unknown (offline media -- we do not guess)."""
    if clip.media_id not in ctx.registry:
        return None
    info = ctx.registry.get(clip.media_id).info
    if info is None or info.is_still or info.duration_seconds is None:
        return None
    return info.duration(ctx.rate)


def head_room(ctx: EditContext, clip: Clip) -> Optional[RationalTime]:
    """Timeline-time available for extending a clip's head. None = unlimited."""
    limit = source_limit(ctx, clip)
    speed = abs(clip.speed)
    if clip.is_reversed:
        if limit is None:
            return None
        return (limit - clip.source_out_exclusive) / speed
    return clip.source_in / speed


def tail_room(ctx: EditContext, clip: Clip) -> Optional[RationalTime]:
    """Timeline-time available for extending a clip's tail. None = unlimited."""
    limit = source_limit(ctx, clip)
    speed = abs(clip.speed)
    if clip.is_reversed:
        return clip.source_in / speed
    if limit is None:
        return None
    return (limit - clip.source_out_exclusive) / speed


def _one_frame(ctx: EditContext) -> RationalTime:
    return RationalTime.from_frames(1, ctx.rate)


def _clamp(delta: RationalTime, lower: Optional[RationalTime],
           upper: Optional[RationalTime],
           lower_reason: str, upper_reason: str) -> Tuple[RationalTime, Optional[str]]:
    if lower is not None and delta < lower:
        return lower, lower_reason
    if upper is not None and delta > upper:
        return upper, upper_reason
    return delta, None


def _min_optional(*values: Optional[RationalTime]) -> Optional[RationalTime]:
    present = [v for v in values if v is not None]
    return min(present) if present else None


Bound = Tuple[Optional[RationalTime], str]


def _tightest(bounds: Iterable[Bound], upper: bool) -> Bound:
    """Pick the binding constraint out of several, keeping its reason.

    For an upper bound the tightest is the smallest; for a lower bound the
    largest (i.e. the least negative). Carrying the reason along is what lets
    the UI say "trim stopped: out of media" instead of just refusing.
    """
    present = [(value, reason) for value, reason in bounds if value is not None]
    if not present:
        return None, ""
    pick = min if upper else max
    return pick(present, key=lambda item: item[0].to_seconds())


# ---------------------------------------------------------------------------
# low-level model surgery (used by the commands below)
# ---------------------------------------------------------------------------

def _set_head(clip: Clip, delta: RationalTime) -> None:
    """Move a clip's in point by `delta` of timeline time (source-consuming)."""
    source_delta = delta * abs(clip.speed)
    new_duration = clip.source_range.duration - source_delta
    if clip.is_reversed:
        clip.source_range = TimeRange(clip.source_range.start_time, new_duration)
    else:
        clip.source_range = TimeRange(clip.source_range.start_time + source_delta, new_duration)


def _set_tail(clip: Clip, delta: RationalTime) -> None:
    """Move a clip's out point by `delta` of timeline time."""
    source_delta = delta * abs(clip.speed)
    new_duration = clip.source_range.duration + source_delta
    if clip.is_reversed:
        clip.source_range = TimeRange(clip.source_range.start_time - source_delta, new_duration)
    else:
        clip.source_range = TimeRange(clip.source_range.start_time, new_duration)


def split_clip(track: Track, clip: Clip, at: RationalTime) -> Optional[Tuple[Clip, Clip]]:
    """Cut one clip in two at `at`. Returns None if `at` is not inside it.

    The two halves keep frame-exact continuity: the right half starts on the
    source frame the left half would have shown next, so a blade followed by
    deleting nothing is a true no-op picture-wise.
    """
    if not (clip.start < at < clip.end):
        return None
    left_duration = at - clip.start
    right_duration = clip.end - at
    speed = abs(clip.speed)

    left = clip
    right = clip.with_(clip_id=new_id("clip"), start=at)

    if clip.is_reversed:
        boundary = clip.source_range.end_time_exclusive - left_duration * speed
        left.source_range = TimeRange(boundary, left_duration * speed)
        right.source_range = TimeRange(clip.source_range.start_time, right_duration * speed)
    else:
        boundary = clip.source_range.start_time + left_duration * speed
        left.source_range = TimeRange(clip.source_range.start_time, left_duration * speed)
        right.source_range = TimeRange(boundary, right_duration * speed)

    # markers travel with the half they sit in
    left_markers, right_markers = [], []
    for marker in clip.markers:
        (right_markers if marker.time >= at else left_markers).append(marker)
    left.markers = left_markers
    right.markers = right_markers

    track.clips.insert(track.index_of(left.clip_id) + 1, right)
    track.sort()
    return left, right


def ripple_track(track: Track, from_time: RationalTime, delta: RationalTime,
                 exclude: Seq[str] = ()) -> List[Clip]:
    """Shift every clip starting at or after `from_time` by `delta`."""
    moved = []
    for clip in track.clips:
        if clip.clip_id in exclude:
            continue
        if clip.start >= from_time:
            clip.start = clip.start + delta
            moved.append(clip)
    track.sort()
    return moved


def ripple_tracks_for(ctx: EditContext, primary: Track,
                      respect_sync_lock: bool = True) -> List[Track]:
    """Which tracks move when an edit ripples.

    The primary track always moves. Others move if they are sync-locked and
    unlocked -- that is what keeps music and dialogue in sync when a cut is
    inserted, and it is the behaviour every editor expects by default.
    """
    tracks = [primary]
    if respect_sync_lock:
        for track in ctx.sequence.tracks:
            if track is primary or track.locked or not track.sync_lock:
                continue
            tracks.append(track)
    return tracks


def _require_unlocked(track: Track) -> None:
    if track.locked:
        raise CommandError(f"track {track.name!r} is locked")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

class AddClip(Command):
    """Place a clip on a track. Refuses to overlap existing material."""

    def __init__(self, track_id: str, clip: Clip, label: str = ""):
        super().__init__(label or f"Add {clip.name or 'clip'}")
        self.track_id = track_id
        self.clip = clip

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return [self.track_id]

    def apply(self, ctx: EditContext) -> Clip:
        track = ctx.sequence.track(self.track_id)
        _require_unlocked(track)
        track.place(self.clip)
        return self.clip


class RemoveClips(Command):
    """Lift (leave a gap) or extract (close the gap) whole clips."""

    def __init__(self, clip_ids: Seq[str], ripple: bool = False, label: str = ""):
        super().__init__(label or ("Ripple delete" if ripple else "Delete"))
        self.clip_ids = list(clip_ids)
        self.ripple = ripple

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        ids: List[str] = []
        for clip_id in self.clip_ids:
            track, _ = ctx.sequence.find_clip(clip_id)
            for candidate in (ripple_tracks_for(ctx, track) if self.ripple else [track]):
                if candidate.track_id not in ids:
                    ids.append(candidate.track_id)
        return ids

    def apply(self, ctx: EditContext) -> List[str]:
        removed: List[str] = []
        # Delete late-to-early so earlier ripples do not invalidate later ones.
        targets = []
        for clip_id in self.clip_ids:
            track, clip = ctx.sequence.find_clip(clip_id)
            _require_unlocked(track)
            targets.append((track, clip))
        targets.sort(key=lambda tc: tc[1].start.to_seconds(), reverse=True)

        for track, clip in targets:
            start, duration = clip.start, clip.duration
            track.remove(clip.clip_id)
            removed.append(clip.clip_id)
            if self.ripple:
                for other in ripple_tracks_for(ctx, track):
                    ripple_track(other, start + duration, -duration)
        return removed


class Blade(Command):
    """Razor: split every clip under the playhead on the given tracks."""

    def __init__(self, at: RationalTime, track_ids: Optional[Seq[str]] = None,
                 label: str = "Blade"):
        super().__init__(label)
        self.at = at
        self.track_ids = list(track_ids) if track_ids is not None else None

    def _tracks(self, ctx: EditContext) -> List[Track]:
        if self.track_ids is None:
            return [t for t in ctx.sequence.targeted_tracks()]
        return [ctx.sequence.track(t) for t in self.track_ids]

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return [t.track_id for t in self._tracks(ctx)]

    def apply(self, ctx: EditContext) -> List[str]:
        new_ids: List[str] = []
        for track in self._tracks(ctx):
            if track.locked:
                continue
            clip = track.clip_at(self.at)
            if clip is None:
                continue
            halves = split_clip(track, clip, self.at)
            if halves:
                new_ids.append(halves[1].clip_id)
        return new_ids


class LiftRange(Command):
    """Clear a time range, leaving a gap (partial clips are trimmed/split)."""

    ripple = False

    def __init__(self, span: TimeRange, track_ids: Optional[Seq[str]] = None,
                 label: str = ""):
        super().__init__(label or ("Extract" if self.ripple else "Lift"))
        self.span = span
        self.track_ids = list(track_ids) if track_ids is not None else None

    def _tracks(self, ctx: EditContext) -> List[Track]:
        if self.track_ids is None:
            return [t for t in ctx.sequence.targeted_tracks() if not t.locked]
        return [ctx.sequence.track(t) for t in self.track_ids]

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        ids = [t.track_id for t in self._tracks(ctx)]
        if self.ripple:
            for track in self._tracks(ctx):
                for other in ripple_tracks_for(ctx, track):
                    if other.track_id not in ids:
                        ids.append(other.track_id)
        return ids

    def apply(self, ctx: EditContext) -> None:
        if self.span.is_empty:
            return
        for track in self._tracks(ctx):
            if track.locked:
                continue
            _clear_range(track, self.span)
        if self.ripple:
            rippled: set[str] = set()
            for track in self._tracks(ctx):
                for other in ripple_tracks_for(ctx, track):
                    if other.track_id in rippled or other.locked:
                        continue
                    rippled.add(other.track_id)
                    ripple_track(other, self.span.end_time_exclusive, -self.span.duration)


class ExtractRange(LiftRange):
    """Clear a time range and close the gap (ripple)."""

    ripple = True


def _clear_range(track: Track, span: TimeRange) -> None:
    """Remove everything inside `span` on one track, trimming straddlers."""
    for clip in list(track.clips):
        overlap = clip.range.intersection(span)
        if overlap is None:
            continue
        if span.contains(clip.range):
            track.remove(clip.clip_id)
            continue
        if clip.range.contains(span) and clip.start < span.start_time and span.end_time_exclusive < clip.end:
            # span sits strictly inside the clip: split, then drop the middle
            halves = split_clip(track, clip, span.start_time)
            assert halves is not None
            right = halves[1]
            second = split_clip(track, right, span.end_time_exclusive)
            assert second is not None
            track.remove(second[0].clip_id)
            continue
        if clip.start < span.start_time:          # clip's tail is inside span
            _set_tail(clip, span.start_time - clip.end)
        else:                                     # clip's head is inside span
            delta = span.end_time_exclusive - clip.start
            _set_head(clip, delta)
            clip.start = clip.start + delta
    track.sort()


class Overwrite(Command):
    """Drop a clip onto a track, replacing whatever it lands on.

    Sequence length never changes except by extending the end -- this is the
    edit you use when picture timing is already locked.
    """

    def __init__(self, clip: Clip, track_id: str, at: Optional[RationalTime] = None,
                 label: str = ""):
        super().__init__(label or f"Overwrite {clip.name or 'clip'}")
        self.clip = clip
        self.track_id = track_id
        self.at = at

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return [self.track_id]

    def apply(self, ctx: EditContext) -> Clip:
        track = ctx.sequence.track(self.track_id)
        _require_unlocked(track)
        if self.at is not None:
            self.clip.start = self.at
        _clear_range(track, self.clip.range)
        track.place(self.clip)
        return self.clip


class Insert(Command):
    """Push everything at and after the edit point later, then place the clip.

    Straddling clips are split at the insert point, so an insert never
    swallows part of a shot.
    """

    def __init__(self, clip: Clip, track_id: str, at: Optional[RationalTime] = None,
                 ripple_all: bool = True, label: str = ""):
        super().__init__(label or f"Insert {clip.name or 'clip'}")
        self.clip = clip
        self.track_id = track_id
        self.at = at
        self.ripple_all = ripple_all

    def _tracks(self, ctx: EditContext) -> List[Track]:
        primary = ctx.sequence.track(self.track_id)
        return ripple_tracks_for(ctx, primary, respect_sync_lock=self.ripple_all)

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return [t.track_id for t in self._tracks(ctx)]

    def apply(self, ctx: EditContext) -> Clip:
        primary = ctx.sequence.track(self.track_id)
        _require_unlocked(primary)
        at = self.at if self.at is not None else self.clip.start
        self.clip.start = at
        duration = self.clip.duration

        for track in self._tracks(ctx):
            if track.locked:
                continue
            straddler = track.clip_at(at)
            if straddler is not None and straddler.start < at:
                split_clip(track, straddler, at)
            ripple_track(track, at, duration)
        primary.place(self.clip)
        return self.clip


class MoveClip(Command):
    """Move a clip in time and/or to another track.

    `mode` decides what happens when the destination is occupied:
      "refuse"    -- raise, leaving the timeline untouched (safe default)
      "overwrite" -- clear the destination first
    """

    def __init__(self, clip_id: str, to_start: Optional[RationalTime] = None,
                 to_track_id: Optional[str] = None, mode: str = "refuse",
                 move_linked: bool = True, label: str = "Move"):
        super().__init__(label)
        self.clip_id = clip_id
        self.to_start = to_start
        self.to_track_id = to_track_id
        self.mode = mode
        self.move_linked = move_linked

    def _plan(self, ctx: EditContext) -> List[Tuple[Track, Clip, Track]]:
        track, clip = ctx.sequence.find_clip(self.clip_id)
        destination = ctx.sequence.track(self.to_track_id) if self.to_track_id else track
        plan = [(track, clip, destination)]
        if self.move_linked and clip.link_id:
            for other_track, other in ctx.sequence.linked_clips(clip.link_id):
                if other.clip_id != clip.clip_id:
                    plan.append((other_track, other, other_track))
        return plan

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        ids: List[str] = []
        for source, _, destination in self._plan(ctx):
            for track in (source, destination):
                if track.track_id not in ids:
                    ids.append(track.track_id)
        return ids

    def apply(self, ctx: EditContext) -> RationalTime:
        plan = self._plan(ctx)
        primary_track, primary_clip, _ = plan[0]
        target = self.to_start if self.to_start is not None else primary_clip.start
        if target.value < 0:
            target = RationalTime.zero(ctx.rate)
        offset = target - primary_clip.start

        for source, clip, destination in plan:
            new_range = TimeRange(clip.start + offset, clip.duration)
            if new_range.start_time.value < 0:
                raise CommandError("move would push a clip before the start of the timeline")
            _require_unlocked(source)
            _require_unlocked(destination)
            if not destination.free_space_at(new_range, ignore=[clip.clip_id]):
                if self.mode == "refuse":
                    raise OverlapError(
                        f"cannot move {clip.name or clip.clip_id} to {new_range}: space is occupied")
                if self.mode != "overwrite":
                    raise CommandError(f"unknown move mode {self.mode!r}")

        for source, clip, destination in plan:
            source.remove(clip.clip_id)
            clip.start = clip.start + offset
            if self.mode == "overwrite":
                _clear_range(destination, clip.range)
            destination.place(clip)
        return offset


class Trim(Command):
    """Trim one edge of a clip, optionally rippling everything after it."""

    def __init__(self, clip_id: str, edge: str, delta: RationalTime,
                 ripple: bool = False, label: str = ""):
        if edge not in (HEAD, TAIL):
            raise CommandError(f"edge must be {HEAD!r} or {TAIL!r}, got {edge!r}")
        super().__init__(label or f"{'Ripple ' if ripple else ''}Trim {edge}")
        self.clip_id = clip_id
        self.edge = edge
        self.delta = delta
        self.ripple = ripple

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        track, _ = ctx.sequence.find_clip(self.clip_id)
        if not self.ripple:
            return [track.track_id]
        return [t.track_id for t in ripple_tracks_for(ctx, track)]

    def apply(self, ctx: EditContext) -> TrimResult:
        track, clip = ctx.sequence.find_clip(self.clip_id)
        _require_unlocked(track)
        before, after = track.neighbours(clip.clip_id)
        frame = _one_frame(ctx)
        old_end = clip.end

        if self.edge == HEAD:
            # delta > 0 shortens from the head; delta < 0 extends it.
            upper = clip.duration - frame                     # keep one frame alive
            room = head_room(ctx, clip)
            lower_bounds: List[Bound] = [
                (None if room is None else -room, LIMIT_MEDIA)]
            if not self.ripple:
                # a normal trim can only extend into empty timeline
                floor_time = before.end if before is not None else RationalTime.zero(ctx.rate)
                lower_bounds.append((-(clip.start - floor_time), LIMIT_NEIGHBOUR))
            lower, lower_reason = _tightest(lower_bounds, upper=False)

            delta, limited = _clamp(self.delta, lower, upper, lower_reason, LIMIT_LENGTH)
            if delta.value != 0:
                _set_head(clip, delta)
                if self.ripple:
                    # the clip's head stays put; everything past its old tail
                    # closes up (or opens out) by the same amount
                    for other in ripple_tracks_for(ctx, track):
                        if other.locked:
                            continue
                        ripple_track(other, old_end, -delta, exclude=[clip.clip_id])
                else:
                    clip.start = clip.start + delta
        else:
            # delta > 0 lengthens the tail; delta < 0 shortens it.
            lower = -(clip.duration - frame)
            upper_bounds: List[Bound] = [(tail_room(ctx, clip), LIMIT_MEDIA)]
            if not self.ripple and after is not None:
                upper_bounds.append((after.start - clip.end, LIMIT_NEIGHBOUR))
            upper, upper_reason = _tightest(upper_bounds, upper=True)

            delta, limited = _clamp(self.delta, lower, upper, LIMIT_LENGTH, upper_reason)
            if delta.value != 0:
                _set_tail(clip, delta)
                if self.ripple:
                    for other in ripple_tracks_for(ctx, track):
                        if other.locked:
                            continue
                        ripple_track(other, old_end, delta, exclude=[clip.clip_id])

        track.sort()
        return TrimResult(self.delta, delta, limited, [clip.clip_id])


class Roll(Command):
    """Move the cut between two adjacent clips without moving anything else."""

    def __init__(self, track_id: str, at: RationalTime, delta: RationalTime,
                 label: str = "Roll"):
        super().__init__(label)
        self.track_id = track_id
        self.at = at
        self.delta = delta

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return [self.track_id]

    def apply(self, ctx: EditContext) -> TrimResult:
        track = ctx.sequence.track(self.track_id)
        _require_unlocked(track)
        left = next((c for c in track.clips if c.end == self.at), None)
        right = next((c for c in track.clips if c.start == self.at), None)
        if left is None or right is None:
            raise CommandError(f"no butt-joined edit point at {self.at} on {track.name}")

        frame = _one_frame(ctx)
        # rolling right: the outgoing clip needs tail handles, the incoming
        # clip needs frames to give up (and vice versa rolling left)
        upper, upper_reason = _tightest(
            [(tail_room(ctx, left), LIMIT_MEDIA),
             (right.duration - frame, LIMIT_LENGTH)], upper=True)
        head = head_room(ctx, right)
        lower, lower_reason = _tightest(
            [(None if head is None else -head, LIMIT_MEDIA),
             (-(left.duration - frame), LIMIT_LENGTH)], upper=False)

        delta, limited = _clamp(self.delta, lower, upper, lower_reason, upper_reason)
        if delta.value != 0:
            _set_tail(left, delta)
            _set_head(right, delta)
            right.start = right.start + delta
            track.sort()
        return TrimResult(self.delta, delta, limited, [left.clip_id, right.clip_id])


class Slip(Command):
    """Shift the content inside a clip; its position and length never change."""

    def __init__(self, clip_id: str, delta: RationalTime, label: str = "Slip"):
        super().__init__(label)
        self.clip_id = clip_id
        self.delta = delta

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        track, _ = ctx.sequence.find_clip(self.clip_id)
        return [track.track_id]

    def apply(self, ctx: EditContext) -> TrimResult:
        track, clip = ctx.sequence.find_clip(self.clip_id)
        _require_unlocked(track)
        # positive delta = show later material
        forward_room = tail_room(ctx, clip)
        backward_room = head_room(ctx, clip)
        lower = None if backward_room is None else -backward_room
        delta, limited = _clamp(self.delta, lower, forward_room, LIMIT_MEDIA, LIMIT_MEDIA)
        if delta.value != 0:
            source_delta = delta * abs(clip.speed)
            if clip.is_reversed:
                source_delta = -source_delta
            clip.source_range = TimeRange(clip.source_range.start_time + source_delta,
                                          clip.source_range.duration)
        return TrimResult(self.delta, delta, limited, [clip.clip_id])


class Slide(Command):
    """Move a clip in time, absorbing the difference into its neighbours."""

    def __init__(self, clip_id: str, delta: RationalTime, label: str = "Slide"):
        super().__init__(label)
        self.clip_id = clip_id
        self.delta = delta

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        track, _ = ctx.sequence.find_clip(self.clip_id)
        return [track.track_id]

    def apply(self, ctx: EditContext) -> TrimResult:
        track, clip = ctx.sequence.find_clip(self.clip_id)
        _require_unlocked(track)
        before, after = track.neighbours(clip.clip_id)
        frame = _one_frame(ctx)

        # A neighbour absorbs the slide only if it is butt-joined; otherwise
        # the clip is sliding through empty space and the gap absorbs it.
        left = before if before is not None and before.end == clip.start else None
        right = after if after is not None and after.start == clip.end else None

        upper_bounds: List[Bound] = []
        lower_bounds: List[Bound] = []

        if left is not None:
            upper_bounds.append((tail_room(ctx, left), LIMIT_MEDIA))       # L must grow
            lower_bounds.append((-(left.duration - frame), LIMIT_LENGTH))  # L must shrink
        else:
            floor_time = before.end if before is not None else RationalTime.zero(ctx.rate)
            lower_bounds.append((-(clip.start - floor_time), LIMIT_NEIGHBOUR))

        if right is not None:
            upper_bounds.append((right.duration - frame, LIMIT_LENGTH))    # R must shrink
            head = head_room(ctx, right)
            lower_bounds.append((None if head is None else -head, LIMIT_MEDIA))
        elif after is not None:
            upper_bounds.append((after.start - clip.end, LIMIT_NEIGHBOUR))

        upper, upper_reason = _tightest(upper_bounds, upper=True)
        lower, lower_reason = _tightest(lower_bounds, upper=False)

        delta, limited = _clamp(self.delta, lower, upper, lower_reason, upper_reason)
        if delta.value != 0:
            if left is not None:
                _set_tail(left, delta)
            if right is not None:
                _set_head(right, delta)
                right.start = right.start + delta
            clip.start = clip.start + delta
            track.sort()
        return TrimResult(self.delta, delta, limited, [clip.clip_id])


class CloseGap(Command):
    """Ripple away the empty space at a point on a track."""

    def __init__(self, track_id: str, at: RationalTime, ripple_all: bool = True,
                 label: str = "Close gap"):
        super().__init__(label)
        self.track_id = track_id
        self.at = at
        self.ripple_all = ripple_all

    def _tracks(self, ctx: EditContext) -> List[Track]:
        primary = ctx.sequence.track(self.track_id)
        return ripple_tracks_for(ctx, primary, respect_sync_lock=self.ripple_all)

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return [t.track_id for t in self._tracks(ctx)]

    def apply(self, ctx: EditContext) -> Optional[TimeRange]:
        track = ctx.sequence.track(self.track_id)
        _require_unlocked(track)
        gap = track.gap_at(self.at, ctx.rate)
        if gap is None or gap.is_empty:
            return None
        for other in self._tracks(ctx):
            if other.locked:
                continue
            ripple_track(other, gap.end_time_exclusive, -gap.duration)
        return gap


class SetClipProperties(Command):
    """Change simple clip attributes (enabled, name, gain, speed, effects)."""

    def __init__(self, clip_id: str, label: str = "Change clip", **changes: Any):
        super().__init__(label)
        self.clip_id = clip_id
        self.changes = changes

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        track, _ = ctx.sequence.find_clip(self.clip_id)
        return [track.track_id]

    def apply(self, ctx: EditContext) -> Clip:
        track, clip = ctx.sequence.find_clip(self.clip_id)
        _require_unlocked(track)
        for key, value in self.changes.items():
            if not hasattr(clip, key):
                raise CommandError(f"clip has no property {key!r}")
            setattr(clip, key, value)
        track.sort()
        return clip


class SetSpeed(Command):
    """Retime a clip. `hold_start` keeps the head pinned (the usual choice)."""

    def __init__(self, clip_id: str, speed: Fraction, ripple: bool = False,
                 label: str = "Change speed"):
        super().__init__(label)
        self.clip_id = clip_id
        self.speed = Fraction(speed)
        self.ripple = ripple

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        track, _ = ctx.sequence.find_clip(self.clip_id)
        if not self.ripple:
            return [track.track_id]
        return [t.track_id for t in ripple_tracks_for(ctx, track)]

    def apply(self, ctx: EditContext) -> RationalTime:
        if self.speed == 0:
            raise CommandError("speed 0 is not a speed; use a freeze frame")
        track, clip = ctx.sequence.find_clip(self.clip_id)
        _require_unlocked(track)
        old_duration = clip.duration
        clip.speed = self.speed
        new_duration = clip.duration
        growth = new_duration - old_duration
        if self.ripple and growth.value != 0:
            for other in ripple_tracks_for(ctx, track):
                ripple_track(other, clip.start + old_duration, growth, exclude=[clip.clip_id])
        elif growth.value > 0:
            # not rippling: do not let the retimed clip run over its neighbour
            _, after = track.neighbours(clip.clip_id)
            if after is not None and clip.end > after.start:
                allowed = after.start - clip.start
                clip.source_range = TimeRange(clip.source_range.start_time,
                                              allowed * abs(clip.speed))
        track.sort()
        return clip.duration


class LinkClips(Command):
    """Tie clips together so they move as one (a take's picture and sound)."""

    def __init__(self, clip_ids: Seq[str], link_id: Optional[str] = None,
                 label: str = "Link clips"):
        super().__init__(label)
        self.clip_ids = list(clip_ids)
        self.link_id = link_id or f"lnk-{uuid.uuid4().hex[:10]}"

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        ids: List[str] = []
        for clip_id in self.clip_ids:
            track, _ = ctx.sequence.find_clip(clip_id)
            if track.track_id not in ids:
                ids.append(track.track_id)
        return ids

    def apply(self, ctx: EditContext) -> str:
        for clip_id in self.clip_ids:
            _, clip = ctx.sequence.find_clip(clip_id)
            clip.link_id = self.link_id
        return self.link_id


class AddMarker(Command):
    """Add a sequence marker (clip markers ride along with their clip)."""

    touches_sequence_state = True

    def __init__(self, at: RationalTime, name: str = "", note: str = "",
                 color: str = "yellow", duration: Optional[RationalTime] = None,
                 label: str = "Add marker"):
        super().__init__(label)
        self.marker = Marker(marker_id=new_id("mrk"), time=at, name=name,
                             note=note, color=color, duration=duration)

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return []

    def apply(self, ctx: EditContext) -> Marker:
        ctx.sequence.markers.append(self.marker)
        ctx.sequence.markers.sort(key=lambda m: m.time.to_seconds())
        return self.marker


class SetInOut(Command):
    """Set the sequence in/out points (the work area for renders)."""

    touches_sequence_state = True

    def __init__(self, in_point: Optional[RationalTime] = None,
                 out_point: Optional[RationalTime] = None, label: str = "Set in/out"):
        super().__init__(label)
        self.in_point = in_point
        self.out_point = out_point

    def affected_track_ids(self, ctx: EditContext) -> Seq[str]:
        return []

    def apply(self, ctx: EditContext) -> TimeRange:
        ctx.sequence.in_point = self.in_point
        ctx.sequence.out_point = self.out_point
        return ctx.sequence.work_range

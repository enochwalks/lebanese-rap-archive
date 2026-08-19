"""Undo/redo correctness.

The headline test is `test_undo_all_restores_the_exact_starting_state`: it
runs a long pseudo-random storm of edits and then undoes every one, asserting
the timeline serializes back to precisely what it was. Undo bugs are the kind
that quietly eat a day's work, so this is the property worth hammering.
"""

import json
import random
from fractions import Fraction

import pytest

from conftest import fake_video_media
from edit_engine import ops
from edit_engine.commands import CommandError, CommandStack, FunctionCommand
from edit_engine.model import make_clip
from edit_engine.timebase import TimeRange


@pytest.fixture
def stack(project):
    return CommandStack(project, verify_declarations=True)


def snapshot(sequence):
    return json.dumps(sequence.to_dict(), sort_keys=True)


class TestUndoRedo:
    def test_undo_restores_and_redo_reapplies(self, ab_timeline, stack):
        sequence, track, clip_a, _ = ab_timeline
        before = snapshot(sequence)
        stack.run(ops.RemoveClips([clip_a.clip_id], ripple=True), sequence)
        after = snapshot(sequence)
        assert stack.undo() == "Ripple delete"
        assert snapshot(sequence) == before
        assert stack.redo() == "Ripple delete"
        assert snapshot(sequence) == after

    def test_redo_does_not_re_execute_the_command(self, ab_timeline, stack):
        """A command that consults something non-deterministic must still redo
        to the state the user saw -- redo replays the recorded result."""
        sequence, track, _, _ = ab_timeline
        rolls = iter([25, 999])

        def wobble(ctx):
            offset = next(rolls)
            ctx.sequence.track(track.track_id).clips[0].start = ctx.sequence.frames(0)
            ctx.sequence.track(track.track_id).clips[1].start = ctx.sequence.frames(offset + 100)

        stack.run(FunctionCommand("Wobble", [track.track_id], wobble), sequence)
        after = snapshot(sequence)
        stack.undo()
        stack.redo()
        assert snapshot(sequence) == after

    def test_new_edit_clears_the_redo_branch(self, ab_timeline, stack):
        sequence, _, clip_a, clip_b = ab_timeline
        stack.run(ops.RemoveClips([clip_a.clip_id]), sequence)
        stack.undo()
        assert stack.can_redo
        stack.run(ops.RemoveClips([clip_b.clip_id]), sequence)
        assert not stack.can_redo

    def test_undo_all_restores_the_exact_starting_state(self, project, sequence):
        """The one that matters: an edit storm, fully reversed."""
        stack = CommandStack(project, history_limit=10_000, verify_declarations=True)
        media = fake_video_media(project, "storm.mp4", seconds=60)
        track = sequence.video_tracks[0]
        for i in range(8):
            track.place(make_clip(media, sequence, sequence.frames(i * 60),
                                  source_in=sequence.frames(300),
                                  duration=sequence.frames(60), name=f"S{i}"))
        start_state = snapshot(sequence)

        rng = random.Random(20260818)
        applied = 0
        for step in range(600):
            clips = list(track.clips)
            if len(clips) < 3:
                # keep the storm supplied with material to chew on
                end = track.duration(sequence.rate)
                stack.run(ops.AddClip(track.track_id,
                                      make_clip(media, sequence, end + sequence.frames(10),
                                                source_in=sequence.frames(300),
                                                duration=sequence.frames(60),
                                                name=f"R{step}")), sequence)
                applied += 1
                continue
            clip = rng.choice(clips)
            delta = sequence.frames(rng.randint(-20, 20))
            choice = rng.choices(
                ["trim_head", "trim_tail", "slip", "slide", "blade", "delete", "move"],
                weights=[4, 4, 3, 3, 2, 1, 2])[0]
            try:
                if choice == "trim_head":
                    stack.run(ops.Trim(clip.clip_id, ops.HEAD, delta,
                                       ripple=rng.random() < 0.5), sequence)
                elif choice == "trim_tail":
                    stack.run(ops.Trim(clip.clip_id, ops.TAIL, delta,
                                       ripple=rng.random() < 0.5), sequence)
                elif choice == "slip":
                    stack.run(ops.Slip(clip.clip_id, delta), sequence)
                elif choice == "slide":
                    stack.run(ops.Slide(clip.clip_id, delta), sequence)
                elif choice == "blade":
                    stack.run(ops.Blade(sequence.frames(rng.randint(0, 480)),
                                        [track.track_id]), sequence)
                elif choice == "delete":
                    stack.run(ops.RemoveClips([clip.clip_id],
                                              ripple=rng.random() < 0.5), sequence)
                else:
                    stack.run(ops.MoveClip(clip.clip_id, sequence.frames(rng.randint(0, 480)),
                                           mode="overwrite"), sequence)
            except CommandError:
                continue           # refused edits must leave no trace either
            applied += 1
            assert sequence.validate(project.registry) == []

        assert applied > 300, "the storm did not actually exercise the engine"
        undone = stack.undo_all()
        assert undone == applied
        assert snapshot(sequence) == start_state

    def test_redo_all_after_undo_all(self, ab_timeline, stack, media):
        sequence, track, clip_a, clip_b = ab_timeline
        stack.run(ops.Trim(clip_a.clip_id, ops.TAIL, -sequence.frames(20)), sequence)
        stack.run(ops.Slip(clip_b.clip_id, sequence.frames(10)), sequence)
        stack.run(ops.Blade(sequence.frames(120), [track.track_id]), sequence)
        final = snapshot(sequence)
        stack.undo_all()
        while stack.redo():
            pass
        assert snapshot(sequence) == final


class TestTransactions:
    def test_a_transaction_undoes_as_one_step(self, ab_timeline, stack, media):
        sequence, track, _, _ = ab_timeline
        before = snapshot(sequence)
        with stack.transaction("Beat cut pass"):
            for frame in (30, 60, 120, 150):
                stack.run(ops.Blade(sequence.frames(frame), [track.track_id]), sequence)
        assert len(track.clips) == 6
        assert stack.undo_label == "Beat cut pass"
        stack.undo()
        assert snapshot(sequence) == before
        assert not stack.can_undo

    def test_a_failing_transaction_rolls_everything_back(self, ab_timeline, stack, media):
        sequence, track, clip_a, _ = ab_timeline
        before = snapshot(sequence)
        with pytest.raises(CommandError):
            with stack.transaction("Half-finished edit"):
                stack.run(ops.Blade(sequence.frames(30), [track.track_id]), sequence)
                track.locked = True
                stack.run(ops.Trim(clip_a.clip_id, ops.TAIL, sequence.frames(5)), sequence)
        track.locked = False
        assert snapshot(sequence) == before
        assert not stack.can_undo

    def test_transaction_widens_to_tracks_touched_later(self, project, sequence, stack, media):
        video, audio = sequence.video_tracks[0], sequence.audio_tracks[0]
        video.place(make_clip(media, sequence, sequence.zero(), duration=sequence.frames(60)))
        audio.place(make_clip(media, sequence, sequence.zero(), duration=sequence.frames(60)))
        before = snapshot(sequence)
        with stack.transaction("Cut picture then sound"):
            stack.run(ops.Blade(sequence.frames(30), [video.track_id]), sequence)
            stack.run(ops.Blade(sequence.frames(30), [audio.track_id]), sequence)
        stack.undo()
        assert snapshot(sequence) == before

    def test_empty_transaction_is_not_recorded(self, ab_timeline, stack):
        sequence, _, _, _ = ab_timeline
        with stack.transaction("Nothing happened"):
            pass
        assert not stack.can_undo


class TestFailureAtomicity:
    def test_a_failed_command_leaves_no_partial_edit(self, ab_timeline, stack, media):
        sequence, track, clip_a, _ = ab_timeline
        before = snapshot(sequence)
        boom = FunctionCommand(
            "Explode", [track.track_id],
            lambda ctx: (ctx.sequence.track(track.track_id).clips.pop(0), 1 / 0)[1])
        with pytest.raises(ZeroDivisionError):
            stack.run(boom, sequence)
        assert snapshot(sequence) == before

    def test_undeclared_track_mutation_is_caught(self, ab_timeline, stack, media):
        """A command that edits a track it did not declare would undo wrong."""
        sequence, track, _, _ = ab_timeline
        other = sequence.video_tracks[1]
        other.place(make_clip(media, sequence, sequence.zero(), duration=sequence.frames(30)))
        stack._refresh_fingerprints(sequence)

        sneaky = FunctionCommand(
            "Sneaky", [track.track_id],
            lambda ctx: ctx.sequence.track(other.track_id).clips.clear())
        with pytest.raises(CommandError, match="undeclared"):
            stack.run(sneaky, sequence)


class TestHistoryHousekeeping:
    def test_dirty_flag_tracks_unsaved_work(self, ab_timeline, stack):
        sequence, _, clip_a, _ = ab_timeline
        assert not stack.dirty
        stack.run(ops.RemoveClips([clip_a.clip_id]), sequence)
        assert stack.dirty
        stack.mark_saved()
        assert not stack.dirty

    def test_history_is_capped(self, ab_timeline, project):
        sequence, track, _, _ = ab_timeline
        small = CommandStack(project, history_limit=5)
        for frame in range(1, 30):
            small.run(ops.Blade(sequence.frames(frame * 3), [track.track_id]), sequence)
        assert len(small.history()) == 5

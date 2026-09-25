"""Music-keypoint snapping. Dialogue cuts are not moved."""

from __future__ import annotations

import pytest

from conductor.assembly.beats import BeatGrid
from conductor.assembly.snap import (
    WordSpan,
    choose_music_offset,
    dialogue_times_unchanged,
    keypoints_from_grid,
    montage_durations,
    music_offset_candidates,
    snap_time,
    snap_visual_edits,
    VisualEdit,
)
from conductor.lint import lint_plan
from conductor.router import Router
from eval.timeline_lint import (
    PROFILE,
    assert_tamper_caught,
    harmony_plan,
    rank_snapped_over_offbeat,
    shift_cutaway_offbeat,
)


def _click(duration: float = 8.0) -> BeatGrid:
    """120 BPM: a beat every 0.5s, an accent (downbeat) every 2s."""
    beats = [round(i * 0.5, 6) for i in range(int(duration / 0.5) + 1) if i * 0.5 < duration]
    return BeatGrid(bpm=120.0, beats=beats, downbeats=beats[::4], onsets=[], source="synthetic", confidence=1.0)


def test_cutaway_out_snaps_to_the_downbeat():
    points = keypoints_from_grid(_click())
    assert snap_time(3.93, points, window=0.25) == pytest.approx(4.0)


def test_cut_outside_the_window_stays():
    points = keypoints_from_grid(_click())
    # 0.4s from the nearest remaining beat, which is outside the 0.25s window.
    distant = [point for point in points if abs(point.time - 0.4) >= 0.4 - 1e-6]
    assert min(abs(point.time - 0.4) for point in distant) == pytest.approx(0.4)
    assert snap_time(0.4, distant, window=0.25) == 0.4


def test_dialogue_cuts_stay_put():
    points = keypoints_from_grid(_click())
    edits = [
        VisualEdit("line", 1.37, "dialogue", dialogue=True),
        VisualEdit("cutaway-out", 3.93, "cutaway"),
    ]
    snapped = snap_visual_edits(edits, points, window=0.25)
    assert snapped[0].time == 1.37
    assert snapped[1].time == pytest.approx(4.0)
    assert dialogue_times_unchanged(edits, snapped)


def test_snap_does_not_cross_a_spoken_word_or_leave_the_host():
    points = keypoints_from_grid(_click())
    assert snap_time(3.93, points, window=0.25, words=[WordSpan(3.95, 4.05)]) == 3.93
    assert snap_time(3.93, points, window=0.25, hi=3.96) == 3.93
    assert snap_time(3.93, points, window=0.25, accept=lambda _moment: False) == 3.93


def test_no_grid_is_a_noop():
    assert keypoints_from_grid(None) == []
    assert snap_time(3.93, [], window=0.25) == 3.93
    assert montage_durations(4.0, []) == [4.0]


def test_montage_durations_sum_to_the_section():
    points = keypoints_from_grid(_click())
    high = montage_durations(8.0, points, energy=0.9, min_shot=0.2)
    low = montage_durations(8.0, points, energy=0.1, min_shot=0.2)
    assert sum(high) == pytest.approx(8.0)
    assert sum(low) == pytest.approx(8.0)
    assert len(high) > len(low)


def test_downbeat_outranks_a_nearby_onset():
    grid = _click(8.0)
    grid.onsets = [3.96]
    points = keypoints_from_grid(grid, onset_strength=[1.0])
    assert snap_time(3.93, points, window=0.25) == pytest.approx(4.0)


def test_music_offset_is_a_veto_among_offered_times():
    grid = _click()
    offered = music_offset_candidates(grid, [0.5, 2.5])
    assert offered and len(offered) <= 3
    kept = choose_music_offset(grid, [0.5, 2.5])
    assert kept is not None and kept.source_start == offered[0].source_start

    class _Veto:
        def decide(self, asks, **_kwargs):
            second = list(asks[0].options)[1]

            class _Answer:
                value = second

            return [_Answer()], []

    vetoed = choose_music_offset(grid, [0.5, 2.5], router=_Veto())
    assert vetoed is not None
    assert vetoed.source_start == offered[1].source_start

    class _Invent:
        def decide(self, asks, **_kwargs):
            class _Answer:
                value = "9.9999"

            return [_Answer()], []

    rejected = choose_music_offset(grid, [0.5, 2.5], router=_Invent())
    assert rejected is not None and rejected.source_start == offered[0].source_start

    dry = choose_music_offset(grid, [0.5, 2.5], router=Router(live=False), brief="a short cut")
    assert dry is not None
    assert dry.source_start in {item.source_start for item in offered}


def test_offbeat_cutaway_fails_harmony_and_loses_to_the_snapped_plan():
    snapped = harmony_plan()
    report = lint_plan(snapped, profile=PROFILE, check_media=False)
    assert report.stats["av_harmony"] == 1.0
    assert not any(item.code == "av_harmony" for item in report.soft)
    assert_tamper_caught(shift_cutaway_offbeat(snapped), code="av_harmony", soft=True)
    assert rank_snapped_over_offbeat() == 0

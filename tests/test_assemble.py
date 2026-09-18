"""Tier 3: never overrun, never drift, never reorder the review queue."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import BRIEF

from cutmcp import assemble, decide

TARGETS = (60.0, 180.0, 600.0)
PENALTIES = (0.2, 0.6, 1.5)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def timeline(media, decisions):
    return assemble.build(media, decisions, 180.0, BRIEF)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("cut_penalty", PENALTIES)
def test_budget_never_overruns(media, decisions, target, cut_penalty):
    """Gate 5. Overshooting a delivery target is a real failure."""
    tl = assemble.build(media, decisions, target, BRIEF, cut_penalty=cut_penalty)
    assert tl.duration <= target
    assert tl.duration == pytest.approx(sum(c.duration for c in tl.clips))
    assert tl.duration > target * 0.75, "undershooting this far is a regression, not rounding"


def test_build_is_deterministic(media, decisions):
    """Gate 6. Identical scores, identical timeline, byte for byte."""
    a = assemble.build(media, decisions, 180.0, BRIEF)
    b = assemble.build(media, decisions, 180.0, BRIEF)
    assert a.to_json() == b.to_json()
    assert a.timeline_id == b.timeline_id


def test_more_penalty_means_fewer_clips(media, decisions):
    """Gate 7. cut_penalty is the tightness/smoothness knob."""
    for target in TARGETS:
        counts = [
            len(assemble.build(media, decisions, target, BRIEF, cut_penalty=p).clips)
            for p in PENALTIES
        ]
        assert counts == sorted(counts, reverse=True), f"{target}s: {counts}"
        assert counts[0] > counts[-1], f"{target}s: knob did nothing"


def test_review_queue_is_least_confident_first(timeline):
    """Gate 8."""
    q = assemble.review_queue(timeline, limit=20)
    assert len(q) == min(20, len(timeline.cuts))
    assert [c.confidence for c in q] == sorted(c.confidence for c in q)
    full = assemble.review_queue(timeline, limit=10_000)
    assert len(full) == len(timeline.cuts)
    assert [c.idx for c in full[:20]] == [c.idx for c in q], "limit must not reorder"
    assert assemble.review_queue(timeline, limit=0) == []


@pytest.mark.parametrize(
    "seconds", [0.0, 0.04, 1.0, 1.9999, 59.98, 60.0, 3599.96, 3661.5, 12345.678]
)
def test_timecode_round_trips_within_one_frame(seconds):
    """Gate 9."""
    tc = assemble._tc(seconds, 24)
    assert len(tc) == 11 and tc.count(":") == 3
    assert abs(assemble._untc(tc, 24) - seconds) <= 1 / 24


def test_timeline_id_is_stable_across_processes(media, media_path):
    """Gate 10. blake2b, never hash() — which is salted per process."""
    assert (
        assemble._timeline_id(
            "deadbeefcafe0000", BRIEF, 180.0, 0.25, 0.6, [0, 1, 2, 5, 8, 13, 21]
        )
        == "2fbf5315066823c9f071"
    )
    assert assemble._timeline_id("deadbeefcafe0000", "", 60.0, 0.25, 1.5, []) == "eb0ecce78f20fc45631c"

    here = assemble.build(
        media, decide.run(media, BRIEF), 180.0, BRIEF
    ).timeline_id
    env = {**os.environ, "PYTHONHASHSEED": "1"}
    script = (
        "import sys;from cutmcp import extract,decide,assemble;"
        "m=extract.ingest(sys.argv[1]);"
        "print(assemble.build(m,decide.run(m,sys.argv[2],use_cache=False),180.0,sys.argv[2]).timeline_id)"
    )
    r = subprocess.run(
        [sys.executable, "-c", script, str(media_path), BRIEF],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == here, "id, decisions or mock judge drifted across processes"


def test_clips_are_contiguous_runs_in_source_order(media, timeline):
    by_idx = {s.idx: s for s in media.segments}
    assert timeline.clips
    for prev, clip in zip(timeline.clips, timeline.clips[1:]):
        assert clip.first_segment > prev.last_segment + 1, "adjacent runs should be one clip"
        assert clip.start >= prev.end
    for clip in timeline.clips:
        assert clip.start == by_idx[clip.first_segment].start
        assert clip.end == by_idx[clip.last_segment].end
        assert clip.duration == pytest.approx(clip.end - clip.start)


def test_a_cut_is_recorded_at_every_run_boundary(media, decisions, timeline):
    dec = {d.idx: d for d in decisions}
    assert len(timeline.cuts) == len(timeline.clips)
    playhead = 0.0
    for clip, cut in zip(timeline.clips, timeline.cuts):
        playhead += clip.duration
        assert cut.segment == clip.last_segment
        assert cut.at == pytest.approx(playhead)
        assert cut.source_time == clip.end
        assert cut.cut_quality == dec[clip.last_segment].cut_quality
        assert cut.confidence == dec[clip.last_segment].cutq_conf
        assert cut.trailing_text
    assert timeline.cuts[-1].next_text == "", "nothing follows the last cut"


def test_dropped_segments_are_actually_the_weak_ones(media, decisions):
    tl = assemble.build(media, decisions, 180.0, BRIEF)
    kept = {i for c in tl.clips for i in range(c.first_segment, c.last_segment + 1)}
    val = {d.idx: d.value for d in decisions}
    kept_mean = sum(val[i] for i in kept) / len(kept)
    dropped = [v for i, v in val.items() if i not in kept]
    assert kept_mean > sum(dropped) / len(dropped) * 1.5


def test_select_degrades_gracefully(media, decisions):
    assert assemble.select(media.segments, decisions, 0.0) == []
    assert assemble.select([], [], 180.0) == []
    tiny = assemble.select(media.segments, decisions, 3.0)
    assert len(tiny) <= 2


def test_select_needs_a_decision_for_every_segment(media, decisions):
    with pytest.raises(KeyError):
        assemble.select(media.segments, decisions[:-1], 60.0)


def test_export_writes_json_and_edl(timeline, tmp_path):
    written = assemble.export(timeline, tmp_path, fps=24)
    assert set(written) >= {"json", "edl"}
    assert json.loads(Path(written["json"]).read_text())["timeline_id"] == timeline.timeline_id

    edl = Path(written["edl"]).read_text()
    assert edl.startswith(f"TITLE: CUTMCP {timeline.timeline_id}")
    assert "FCM: NON-DROP FRAME" in edl
    events = [l for l in edl.splitlines() if l[:3].isdigit()]
    assert len(events) == len(timeline.clips)

    rec_out = 0.0
    for line, clip in zip(events, timeline.clips):
        _, _, _, _, si, so, ri, ro = line.split()
        assert assemble._untc(si, 24) == pytest.approx(clip.start, abs=1 / 24)
        assert assemble._untc(so, 24) == pytest.approx(clip.end, abs=1 / 24)
        assert assemble._untc(ri, 24) == pytest.approx(rec_out, abs=1 / 24), "record track has a gap"
        rec_out = assemble._untc(ro, 24)
    assert rec_out == pytest.approx(timeline.duration, abs=len(timeline.clips) / 24)


def test_export_writes_otio_when_available(timeline, tmp_path):
    written = assemble.export(timeline, tmp_path, fps=24)
    otio = pytest.importorskip("opentimelineio")
    assert "otio" in written
    tl = otio.adapters.read_from_file(written["otio"])
    assert len(list(tl.find_clips())) == len(timeline.clips)


def test_exported_edl_parses_back_for_calibration(timeline, tmp_path):
    """The eval harness reads hand-cut EDLs; ours must survive that reader."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("calibrate", REPO / "eval" / "calibrate.py")
    calibrate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate)

    edl = Path(assemble.export(timeline, tmp_path, fps=24)["edl"])
    ranges = calibrate.parse_edl(edl, 24)
    assert len(ranges) == len(timeline.clips)
    for (a, b), clip in zip(ranges, timeline.clips):
        assert a == pytest.approx(clip.start, abs=1 / 24)
        assert b == pytest.approx(clip.end, abs=1 / 24)


def test_timeline_persists_and_reloads(timeline):
    loaded = assemble.load_timeline(timeline.timeline_id)
    assert loaded.to_json() == timeline.to_json()
    with pytest.raises(FileNotFoundError):
        assemble.load_timeline("nope")

"""Assembly rounds: v0, refinement against style metrics, carry-over, taste pins."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conductor.assembly import Adjustments, assemble
from conductor.assembly.refine import refine
from conductor.assembly.synth import make_fixture
from conductor.cli import main
from conductor.iterate import format_report, iterate
from conductor.style import load_style

BRIEF = "A 40-second test of the jevid assembly engine"


@pytest.fixture(scope="module")
def shoot(tmp_path_factory):
    folder = tmp_path_factory.mktemp("shoot")
    make_fixture(folder, use_ffmpeg=False)
    return folder


def test_iterate_assembles_v0_and_refines(shoot, tmp_path):
    result = iterate(
        media=shoot,
        brief=BRIEF,
        out_dir=tmp_path,
        style="byjustinwu",
        assemble=True,
        durations_path=shoot / "durations.json",
        max_rounds=3,
    )
    assert result.mode == "assemble"
    assert result.stop_reason in {"metrics", "no-progress", "max-rounds"}
    assert (tmp_path / "v0").is_dir() and result.starter.name.endswith(".assembled.fcpxml")
    payload = json.loads((tmp_path / "iterate.json").read_text())
    assert payload["protocol"] == "cut-conductor.iterate" and payload["mode"] == "assemble"
    assert payload["rounds"][0]["round"] == 0
    assert payload["style"]["provisional"] is True
    first = payload["rounds"][0]
    if first["failures"]:
        assert first["changes"], "a failing round must say what it changes"
        assert len(payload["rounds"]) >= 2
    if result.stop_reason == "metrics":
        assert result.needs_human is False and payload["rounds"][-1]["failures"] == []
    assert "Shapes" in result.starter.read_text(), "the rectangle layer is graphics, not a second still renderer"
    text = format_report(result)
    assert "jevid: assemble" in text and "v0" in text


def test_later_rounds_reuse_decisions_for_the_same_ranges(shoot, tmp_path):
    iterate(
        media=shoot,
        brief=BRIEF,
        out_dir=tmp_path,
        style="byjustinwu",
        assemble=True,
        durations_path=shoot / "durations.json",
        max_rounds=2,
    )
    payload = json.loads((tmp_path / "iterate.json").read_text())
    if len(payload["rounds"]) < 2:
        pytest.skip("v0 already met every target")
    v1 = json.loads((tmp_path / "v1" / "assembly.json").read_text())
    v0 = json.loads((tmp_path / "v0" / "assembly.json").read_text())
    if [u["source_in_seconds"] for u in v0["units"]] == [u["source_in_seconds"] for u in v1["units"]]:
        assert not [r for r in v1["receipts"] if r["stage"] == "select"], "select was re-asked for unchanged ranges"


def test_refine_maps_failures_to_parameter_changes():
    profile = load_style("byjustinwu")
    fake = SimpleNamespace(
        failures=["asl:montage", "asl:talking", "duration", "on_beat", "subtitle_coverage", "background"],
        metrics={"asl_by_section": {"montage": 0.5, "talking": 6.0}, "duration_seconds": 50.0},
        payload={"target_seconds": 40.0},
    )
    new, changes = refine(Adjustments(), fake, profile)
    assert new.asl_scale["montage"] == pytest.approx(0.8 / 0.5)
    assert new.asl_scale["talking"] == pytest.approx(3.8 / 6.0, abs=1e-3)
    assert new.cover_scale > 1.0
    assert new.target_scale == pytest.approx(0.8)
    assert new.snap_scale == pytest.approx(1.5)
    assert new.subtitle_fill is True
    assert len(changes) >= 5
    again, none = refine(new, SimpleNamespace(failures=["background"], metrics=fake.metrics, payload=fake.payload), profile)
    assert none == [] and again.to_state() == new.to_state()


def test_taste_reject_pins_a_decision(shoot, tmp_path):
    first = assemble(media=shoot, brief=BRIEF, durations_path=shoot / "durations.json", out_dir=tmp_path / "a")
    placed = [i.tags["unit"] for i in first.timeline.spine if i.tags.get("speech")]
    victim = placed[-1]
    taste = tmp_path / "taste.json"
    taste.write_text(
        json.dumps(
            {
                "version": 1,
                "log": [
                    {"event": "reject", "candidate_id": f"{victim}_keep", "action": "keep", "pass": "assembly"}
                ],
            }
        )
    )
    second = assemble(
        media=shoot, brief=BRIEF, durations_path=shoot / "durations.json", out_dir=tmp_path / "b", taste_path=taste
    )
    placed_again = [i.tags["unit"] for i in second.timeline.spine if i.tags.get("speech")]
    assert victim not in placed_again
    pinned = next(d for d in second.payload["decisions"] if d["key"] == f"{victim}_keep")
    assert pinned["source"] == "taste" and pinned["value"] == 0.0


def test_cli_iterate_assemble(shoot, tmp_path, capsys):
    code = main(
        [
            "iterate",
            "--assemble",
            "--media",
            str(shoot),
            "--durations",
            str(shoot / "durations.json"),
            "--brief",
            BRIEF,
            "--out-dir",
            str(tmp_path),
            "--max-rounds",
            "1",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "jevid: assemble" in out and "final" in out


def test_mechanical_iterate_is_unchanged_without_assemble(tmp_path):
    result = iterate(fcpxml="fixtures/sample_interview.fcpxml", brief="tight", out_dir=tmp_path, max_rounds=1)
    assert result.mode == "mechanical"

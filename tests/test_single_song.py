"""One music file must cover the timeline without a hole."""

from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from conductor.assembly import assemble
from conductor.fcpxml import parse_fcpxml
from conductor.lint import lint_plan
from conductor.plan import plan_from_document

ROOT = Path(__file__).resolve().parents[1]
BRIEF = "A short diary about why an edit feels slow, and the one rule that fixes it."


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generate = _load("synth_e2e_shoot_song", ROOT / "scripts" / "synth_e2e_shoot.py").generate


def test_zero_iterate_cuts_say_the_assembly_already_cut():
    from conductor.room import _cuts_line, _markdown

    payload = {
        "cuts_applied": 0,
        "flow": "assemble+iterate",
        "cuts": [],
        "duration": {"before": "00:02:00", "after": "00:02:00"},
        "input": {"name": "shoot", "kind": "media", "contained": None},
        "stop_reason": "no-progress",
        "rules_fired": 3,
        "signals_label": "media",
        "decision_usage": {},
        "rules": {},
        "media_signals": {},
        "flagged": [],
        "open_in_final_cut": "/tmp/out.fcpxml",
        "shadow": "/tmp/out.fcpxml",
        "out_dir": "/tmp",
        "needs_human": False,
        "human_reasons": [],
        "warnings": [],
        "style": "byjustinwu",
    }
    assert "nothing further to trim" in _cuts_line(payload)
    text = _markdown(payload)
    assert "Iterate ran on the assembled timeline" in text


def _assemble(tmp_path: Path, beds: str):
    shoot = tmp_path / "shoot"
    generate(shoot, sidecars=True, beds=beds)
    result = assemble(media=shoot, brief=BRIEF, style="byjustinwu", durations_path=shoot / "durations.json", out_dir=tmp_path / "out")
    return result


def _assert_no_hole(path: Path):
    plan = plan_from_document(parse_fcpxml(path), expects_music=True)
    hard = [item for item in lint_plan(plan, check_media=False).hard if item.code == "music_coverage"]
    assert hard == [], hard
    rows = sorted(plan.music(), key=lambda item: item.timeline_start)
    assert rows
    assert float(rows[0].timeline_start) == pytest.approx(0.0, abs=0.05)
    assert float(rows[-1].timeline_end) == pytest.approx(float(plan.duration), abs=0.05)
    for left, right in zip(rows, rows[1:]):
        assert float(left.timeline_end) >= float(right.timeline_start) - 0.02
    root = ET.parse(path).getroot()
    elements = [
        el for el in root.iter("asset-clip") if str(el.get("audioRole") or "").startswith("music")
    ]
    return rows, elements


@pytest.mark.slow
def test_a_48_second_song_loops_without_a_hole(tmp_path):
    result = _assemble(tmp_path, "short")
    rows, elements = _assert_no_hole(result.fcpxml)
    assert len(rows) >= 2
    assert len(elements) == len(rows)
    for el in elements:
        assert list(el.iter("keyframe")), "each copy is ducked"


@pytest.mark.slow
def test_a_long_song_fades_out_at_the_timeline_end(tmp_path):
    result = _assemble(tmp_path, "long")
    rows, elements = _assert_no_hole(result.fcpxml)
    assert len(rows) == 1
    keys = [k for k in elements[0].iter("keyframe") if str(k.get("value") or "").endswith("dB")]
    assert keys
    assert float(keys[-1].get("value")[:-2]) <= -60


@pytest.mark.slow
def test_a_song_about_as_long_as_the_timeline_covers_it(tmp_path):
    result = _assemble(tmp_path, "matched")
    rows, elements = _assert_no_hole(result.fcpxml)
    assert len(rows) <= 2
    keys = [k for k in elements[-1].iter("keyframe") if str(k.get("value") or "").endswith("dB")]
    assert keys and float(keys[-1].get("value")[:-2]) <= -60

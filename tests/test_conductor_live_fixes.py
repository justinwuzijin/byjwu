"""Fixes from a live room-run: totals, holds, silence, project identity, DTD, display path."""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from conductor.candidates import Candidate
from conductor.decide import Proposal, deletions_for
from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml
from conductor.ingest import ingest
from conductor.iterate import iterate
from conductor.jev import policy
from conductor.room import _across_rounds, room_run
from conductor.run import analyze

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
SELECTS = Path("fixtures/selects")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."
DTD_DIR = Path("conductor/dtd")


def _candidate(cid: str, *, kind: str, pass_name: str, start: int, end: int, signals: dict | None = None) -> Candidate:
    return Candidate(
        id=cid,
        kind=kind,
        label=kind,
        sequence="Cut",
        clip_id="s0c0",
        clip_name="A",
        role=None,
        timeline_start=Fraction(start),
        timeline_end=Fraction(end),
        reason="test",
        transcript="",
        signals=signals or {},
        pass_name=pass_name,
    )


def _proposal(cid: str, *, action: str, disposition: str, pass_name: str, confidence: float = 0.9) -> Proposal:
    return Proposal(
        candidate_id=cid,
        raw_action=action,
        action=action,
        disposition=disposition,
        pass_name=pass_name,
        confidence=confidence,
        risk=0.1,
        color="red",
        needs_human=disposition != "auto",
        eligible=disposition == "auto",
        marker_name=None,
        marker_note=None,
    )


def _project(path: Path) -> ET.Element:
    return next(node for node in ET.parse(path).getroot().iter() if node.tag == "project")


def test_missing_transcript_is_unknown_not_silence():
    unknown, _, _ = policy(
        {"kind": "long_static", "duration_seconds": 20, "signals": {"words_per_second": None}}
    )
    quiet, _, _ = policy(
        {"kind": "long_static", "duration_seconds": 20, "signals": {"words_per_second": 0.0}}
    )
    assert unknown == "mark_review"
    assert quiet == "tighten"


def test_unknown_dialogue_and_other_pass_review_are_not_auto_cut():
    silent = _candidate("gap", kind="silence_gap", pass_name="mechanical", start=0, end=3)
    hold = _candidate(
        "hold",
        kind="long_static",
        pass_name="pacing",
        start=0,
        end=3,
        signals={"words_per_second": None},
    )
    colour = _candidate("colour", kind="colour_role", pass_name="colour", start=10, end=12)
    later = _candidate("later", kind="silence_gap", pass_name="mechanical", start=10, end=12)
    clear = _candidate("clear", kind="silence_gap", pass_name="mechanical", start=20, end=23)
    reviewed = _candidate(
        "reviewed",
        kind="long_static",
        pass_name="pacing",
        start=0,
        end=3,
        signals={"words_per_second": 0.0},
    )
    proposals = [
        _proposal("gap", action="remove", disposition="auto", pass_name="mechanical"),
        _proposal("hold", action="tighten", disposition="auto", pass_name="pacing"),
        _proposal("reviewed", action="tighten", disposition="review", pass_name="pacing"),
        _proposal("colour", action="mark_review", disposition="review", pass_name="colour"),
        _proposal("later", action="remove", disposition="auto", pass_name="mechanical"),
        _proposal("clear", action="remove", disposition="auto", pass_name="mechanical"),
    ]
    candidates = [silent, hold, reviewed, colour, later, clear]
    with pytest.raises(ConductorError, match="no cuts matched"):
        deletions_for(
            [proposals[1]],
            [hold],
            accept=None,
            min_confidence=0.8,
            passes=["pacing"],
            hold=Fraction(4),
        )
    kept = deletions_for(
        [proposals[0], proposals[2], proposals[3], proposals[4], proposals[5]],
        [silent, reviewed, colour, later, clear],
        accept=None,
        min_confidence=0.8,
        passes=["mechanical"],
        hold=Fraction(4),
    )
    assert [item.candidate_id for item in kept] == ["clear"]


def test_project_names_and_uids_are_fresh(tmp_path):
    report = analyze(
        FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path,
        apply=True,
        min_confidence=0.8,
        apply_passes=["mechanical"],
    )
    source = _project(FIXTURE)
    marked = _project(report.out_fcpxml)
    applied = _project(report.out_applied)
    assert marked.get("name") == "Rough Cut v1 marked (byjwu)"
    assert applied.get("name") == "Rough Cut v1 (byjwu)"
    assert marked.get("uid") != source.get("uid")
    assert applied.get("uid") != source.get("uid")
    assert marked.get("uid") != applied.get("uid")

    result = iterate(
        fcpxml=FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "loop",
        max_rounds=2,
    )
    second = _project(Path(result.rounds[1]["shadow"]))
    assert second.get("name") == "Rough Cut v2 marked (byjwu)"
    assert second.get("uid") != applied.get("uid")


def test_room_totals_include_earlier_rounds_and_flag_holds(tmp_path, monkeypatch):
    monkeypatch.delenv("BYJWU_OUT_DISPLAY_ROOT", raising=False)
    result = room_run(FIXTURE, out_root=tmp_path, brief=BRIEF)
    payload = result.payload
    rounds = json.loads((result.out_dir / "iterate.json").read_text(encoding="utf-8"))["rounds"]
    per_round = [len(row.get("cuts") or []) for row in rounds]
    assert per_round[-1] == 0
    assert sum(per_round) > 0
    assert payload["cuts_applied"] == sum(per_round)
    assert payload["rules_fired"] == _across_rounds(rounds)[0]
    assert f"Cuts applied: {payload['cuts_applied']}" in result.markdown
    assert "Cuts applied: 0" not in result.markdown
    holds = [item for item in payload["flagged"] if item.get("kind") == "long_static"]
    assert holds
    hold = holds[0]
    assert hold["timecode"]
    assert hold["length"]
    assert f"{hold['timecode']} ({hold['length']})" in result.markdown
    assert str(result.out_dir) in result.markdown

    monkeypatch.setenv("BYJWU_OUT_DISPLAY_ROOT", "~/Desktop/byjwu-out")
    shown = room_run(FIXTURE, out_root=tmp_path, brief=BRIEF)
    line = next(row for row in shown.markdown.splitlines() if row.startswith("Open in Final Cut:"))
    assert line.startswith(f"Open in Final Cut: ~/Desktop/byjwu-out/{shown.out_dir.name}/")
    assert str(shown.out_dir) not in line
    assert shown.payload["open_in_final_cut"].startswith(str(shown.out_dir))
    assert "Users" not in line


def test_rules_from_every_round_not_only_the_last(tmp_path):
    first = tmp_path / "v1.json"
    last = tmp_path / "v2.json"
    first.write_text(
        json.dumps({"changes": [{"engine_source": "rules"}, {"engine_source": "live"}]}),
        encoding="utf-8",
    )
    last.write_text(json.dumps({"changes": [{"engine_source": "mock"}]}), encoding="utf-8")
    rounds = [
        {"cuts": [{"candidate_id": "a"}, {"candidate_id": "b"}], "json": str(first)},
        {"cuts": [], "json": str(last)},
    ]
    assert _across_rounds(rounds) == (1, 2)
    assert _across_rounds([rounds[-1]]) == (0, 0)


def _validate(path: Path) -> None:
    version = ET.parse(path).getroot().get("version")
    dtd = DTD_DIR / f"FCPXMLv{version.replace('.', '_')}.dtd"
    assert dtd.is_file(), f"no DTD for FCPXML {version}"
    proc = subprocess.run(
        ["xmllint", "--noout", "--dtdvalid", str(dtd), str(path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_generated_outputs_match_their_dtd(tmp_path):
    report = analyze(
        FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "marked",
        apply=True,
        min_confidence=0.8,
        apply_passes=["mechanical"],
    )
    _validate(report.out_fcpxml)
    _validate(report.out_applied)
    assembled = ingest(SELECTS, durations_path=SELECTS / "durations.json", brief=BRIEF, out_dir=tmp_path / "assembled")
    _validate(assembled.starter)
    _validate(assembled.report.out_fcpxml)
    assert (DTD_DIR / "FCPXMLv1_14.dtd").is_file()
    assert (DTD_DIR / "FCPXMLv1_11.dtd").is_file()

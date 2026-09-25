"""Measured rules apply without a model score. A veto becomes a review marker."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conductor.fcpxml import parse_fcpxml
from conductor.passes import collect
from conductor.rules import (
    DEFAULTS,
    evaluate,
    margin_confidence,
    nudge_thresholds,
    resolve_thresholds,
)
from conductor.router import Router
from conductor.run import analyze
from conductor.taste import load_taste, write_taste

FIXTURE = Path("fixtures/real_export_shape.fcpxml")
BRIEF = "A travel vlog. Keep the journey, lose dead air and flash frames."


class _Host:
    def __init__(self, veto: str, confidence: float = 0.5):
        self.veto = veto
        self.confidence = confidence
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        answers = {}
        for key, spec in payload["questions"].items():
            if key.endswith("_veto"):
                answers[key] = {"type": "choice", "choice": self.veto, "confidence": self.confidence}
            elif key.endswith("_risk"):
                answers[key] = {"type": "noul", "noul": 0.5}
            else:
                answers[key] = {
                    "type": "choice",
                    "choice": next(iter(spec["criteria"])),
                    "confidence": self.confidence,
                }
        return httpx.Response(
            200,
            json={
                "id": "gen-rules",
                "model": "typesafe/jev-1.13-20260917",
                "provider": "TypeSafe",
                "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0},
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_DECISION_MODE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-rulestest")
    return monkeypatch


def test_margin_climbs_past_the_threshold():
    assert margin_confidence(1.25, 1.25, under=False) == 0.5
    bare = margin_confidence(100.0, 1.25, under=False)
    slight = margin_confidence(1.3, 1.25, under=False)
    assert bare > slight >= 0.80


def test_threshold_order_is_defaults_then_profile_then_taste_then_override(tmp_path, monkeypatch):
    style = tmp_path / "styles" / "demo"
    style.mkdir(parents=True)
    (style / "profile.json").write_text(json.dumps({
        "schema": "placeholder",
        "rules": {"bare_gap_seconds": 3.0, "max_shot_seconds": {"value": 20}},
    }), encoding="utf-8")
    monkeypatch.setattr("conductor.rules._STYLES", tmp_path / "styles")
    resolved = resolve_thresholds(
        style="demo",
        learned={"bare_gap_seconds": 4.0},
        overrides={"bare_gap_seconds": 5.5},
    )
    assert resolved["bare_gap_seconds"] == 5.5
    assert resolved["max_shot_seconds"] == 20
    assert resolved["silence_rms_threshold"] == DEFAULTS["silence_rms_threshold"]
    assert resolved["silence_padding_seconds"] == 0.5
    assert resolved["silence_min_gap_seconds"] == 0.5


def test_reject_nudges_the_gap_threshold_up_and_persists(tmp_path):
    taste = load_taste(None)
    updated, notes = nudge_thresholds(
        resolve_thresholds(),
        [{"event": "reject", "kind": "silence_gap", "rule": "bare_uncovered_gap"}],
    )
    assert updated["bare_gap_seconds"] > DEFAULTS["bare_gap_seconds"]
    assert notes
    taste.rule_thresholds = {"bare_gap_seconds": updated["bare_gap_seconds"]}
    path = tmp_path / "taste.json"
    write_taste(taste, path)
    loaded = load_taste(path)
    assert loaded.rule_thresholds["bare_gap_seconds"] == pytest.approx(updated["bare_gap_seconds"])
    again = resolve_thresholds(learned=loaded.rule_thresholds)
    assert again["bare_gap_seconds"] == pytest.approx(updated["bare_gap_seconds"])


def test_auto_accept_does_not_move_the_threshold():
    updated, notes = nudge_thresholds(
        resolve_thresholds(),
        [{"event": "accept", "kind": "silence_gap", "source": "auto"}],
    )
    assert updated["bare_gap_seconds"] == DEFAULTS["bare_gap_seconds"]
    assert notes == []


def test_logic_first_cuts_bare_gaps_when_the_model_returns_half(env, tmp_path):
    host = _Host("allow", 0.5)
    with Router(live=True, jev_client=host.client(), decision_mode="logic-first") as router:
        report = analyze(
            FIXTURE,
            brief=BRIEF,
            out_dir=tmp_path,
            router=router,
            apply=True,
            min_confidence=0.8,
            passes=["mechanical"],
        )
    assert host.requests
    vetoes = [
        key
        for payload in host.requests
        for key in payload["questions"]
        if key.endswith("_veto")
    ]
    assert vetoes
    # The host answers every question at 0.5, including the veto. The cut does not wait on that score.
    assert report.cuts_applied == 2
    kinds = {cut["decision_type"] for cut in report.payload["cuts"]}
    assert kinds == {"silence_gap"}
    rules = report.payload["rules"]
    assert rules["mode"] == "logic-first"
    assert rules["applied"] == 2
    assert rules["vetoed"] == []
    fired = [row["rule"] for row in rules["fired"] if row["applied"]]
    assert fired == ["bare_uncovered_gap", "bare_uncovered_gap"]
    shadow = report.out_fcpxml.read_text(encoding="utf-8")
    assert "rule=bare_uncovered_gap" in shadow
    assert "measurement=gap_seconds" in shadow
    assert "threshold=bare_gap_seconds" in shadow
    decisions = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
    assert "bare_uncovered_gap" in decisions
    assert "Cuts applied: 2" in decisions
    applied = parse_fcpxml(report.out_applied)
    gap = next(clip for clip in applied.sequences[0].spine if clip.kind == "gap")
    assert [clip.name for clip in gap.connected_clips if clip.lane is not None] == [
        "broll_a", "broll_b", "broll_c", "broll_d",
    ]
    marks = [
        marker
        for clip in applied.sequences[0].spine
        for marker in clip.markers
        if marker.value.startswith("CC cut")
    ]
    assert len(marks) == 2
    frame = applied.sequences[0].frame_duration
    assert all(marker.duration == frame for marker in marks)
    assert all(marker.note and "rule=bare_uncovered_gap" in marker.note for marker in marks)
    assert all("confidence=" in marker.note for marker in marks)
    assert any("100.1s" in marker.value for marker in marks)
    assert any("12.012s" in marker.value for marker in marks)


def test_model_veto_becomes_a_review_marker(env, tmp_path):
    host = _Host("veto_story", 0.91)
    with Router(live=True, jev_client=host.client(), decision_mode="logic-first") as router:
        report = analyze(
            FIXTURE,
            brief=BRIEF,
            out_dir=tmp_path,
            router=router,
            passes=["mechanical"],
        )
    gaps = [row for row in report.changes if row["kind"] == "silence_gap"]
    assert gaps
    assert all(row["disposition"] == "review" for row in gaps)
    assert all(row["engine_source"] == "veto" for row in gaps)
    assert all("beat the brief still needs" in (row["rationale"] or "") for row in gaps)
    assert report.payload["rules"]["applied"] == 0
    assert len(report.payload["rules"]["vetoed"]) == 2
    shadow = report.out_fcpxml.read_text(encoding="utf-8")
    assert "veto=" in shadow
    assert "rule=bare_uncovered_gap" in shadow
    text = (tmp_path / "DECISIONS.md").read_text(encoding="utf-8")
    assert "Vetoed: 2" in text
    assert "beat the brief still needs" in text


def test_flash_duplicate_and_hold_rules_use_their_thresholds():
    flash = type("C", (), {"kind": "short_clip", "duration": 0.1, "signals": {"frame_seconds": 1 / 24, "flash": True}})()
    hit = evaluate(flash, DEFAULTS)
    assert hit is not None and hit.name == "flash_frame" and hit.measurement < DEFAULTS["flash_frames"]
    longer = type("C", (), {"kind": "short_clip", "duration": 0.4, "signals": {"frame_seconds": 1 / 24, "flash": False}})()
    assert evaluate(longer, DEFAULTS) is None
    duplicate = type("C", (), {
        "kind": "source_reuse",
        "duration": 2,
        "signals": {"identical": True, "source_duration_seconds": 2, "do_not_cut": False},
    })()
    assert evaluate(duplicate, DEFAULTS).name == "exact_duplicate"
    reprise = type("C", (), {"kind": "source_reuse", "duration": 2, "signals": {"identical": False, "do_not_cut": True}})()
    assert evaluate(reprise, DEFAULTS) is None
    hold = type("C", (), {
        "kind": "long_static",
        "duration": 50,
        "signals": {"cue_count": 0, "words_per_second": 0},
    })()
    assert evaluate(hold, DEFAULTS).name == "untrimmed_hold"
    talking = type("C", (), {
        "kind": "long_static",
        "duration": 50,
        "signals": {"cue_count": 4, "words_per_second": 1.2},
    })()
    assert evaluate(talking, DEFAULTS) is None
    silence = type("C", (), {
        "kind": "silence_gap",
        "duration": 2,
        "signals": {"audio": True, "gap_seconds": 2},
    })()
    assert evaluate(silence, DEFAULTS).name == "dead_air"


def test_covered_gap_is_not_a_rule_cut():
    document = parse_fcpxml(FIXTURE)
    found = collect(document.sequences, [], transcript_present=False)
    covered = next(item for item in found if item.kind == "covered_gap")
    assert evaluate(covered, DEFAULTS) is None
    bare = [item for item in found if item.kind == "silence_gap"]
    assert len(bare) == 2
    assert all(evaluate(item, DEFAULTS).name == "bare_uncovered_gap" for item in bare)


def test_model_gated_does_not_apply_a_low_score(env, tmp_path):
    host = _Host("allow", 0.5)
    with Router(live=True, jev_client=host.client(), decision_mode="model-gated") as router:
        report = analyze(
            FIXTURE,
            brief=BRIEF,
            out_dir=tmp_path,
            router=router,
            passes=["mechanical"],
        )
    rows = report.changes + report.payload["kept"]
    gaps = [row for row in rows if row["kind"] == "silence_gap"]
    assert gaps and all(row["disposition"] != "auto" for row in gaps)

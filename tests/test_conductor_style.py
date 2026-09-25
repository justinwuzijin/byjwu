"""Style profiles load, validate, and stay tied to the study that measured them."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from conductor.errors import ConductorError
from conductor.style import (
    SCHEMA,
    STYLES_DIR,
    load_profile,
    measured_mismatches,
    validate_profile,
)
from conductor.taste import _validate_prefs
from cutmcp.jev import MAX_OPTIONS

STUDY = STYLES_DIR / "byjustinwu" / "study"


@pytest.fixture(scope="module")
def profile():
    return load_profile("byjustinwu")


@pytest.fixture(scope="module")
def measured():
    return json.loads((STUDY / "measured.json").read_text())


def _minimal() -> dict:
    return {
        "schema": SCHEMA,
        "style": "tiny",
        "version": "1",
        "sources": [{"video_id": "abc", "weight": 1.0}],
        "params": {
            "rhythm": {
                "montage": {
                    "shot_median_s": {"value": 2.0, "confidence": 0.7, "evidence": "abc@10-40"},
                    "cut_to_beat": {
                        "value": "not_enforced",
                        "confidence": 0.3,
                        "evidence": "null onset test",
                        "decided_by": "jev",
                        "question": "beat_lock",
                    },
                }
            }
        },
        "runtime_questions": [
            {
                "id": "beat_lock",
                "kind": "noul",
                "applies_to": "montage_section",
                "instructions": "Cuts should snap to the beat.",
                "prior": 0.3,
                "sets": "rhythm.montage.cut_to_beat",
            }
        ],
    }


def test_byjustinwu_profile_is_valid(profile):
    assert profile.name == "byjustinwu"
    assert profile.path == STYLES_DIR / "byjustinwu" / "profile.json"
    assert len(list(profile.params())) > 100


def test_every_param_has_evidence_and_confidence(profile):
    for path, param in profile.params():
        assert param["evidence"].strip(), path
        assert 0.0 <= param["confidence"] <= 1.0, path


def test_profile_agrees_with_measured_json(profile, measured):
    assert measured_mismatches(profile, measured) == []


def test_headline_values_are_the_measured_ones(profile, measured):
    assert profile.value("graphics.chapter_card.hold_s") == measured["cards"]["chapter_card_hold_s"]["median"]
    assert profile.value("rhythm.montage.shot_median_s") == measured["rhythm"]["by_section"]["montage"]["thr0p3"]["median"]
    assert profile.value("mix.montage_lufs") == measured["levels"]["by_section"]["montage"]["momentary_median_lufs"]
    assert profile.value("music.under_talking") == "none"


def test_jev_params_point_at_questions(profile):
    qids = {q["id"] for q in profile.data["runtime_questions"]}
    jev = [(path, p) for path, p in profile.params() if p.get("decided_by") == "jev"]
    assert jev
    for path, param in jev:
        assert param["question"] in qids, path


def test_questions_build_with_cutmcp_constructors(profile):
    questions = profile.questions("proj_")
    assert set(questions) == {f"proj_{q['id']}" for q in profile.data["runtime_questions"]}
    for key, spec in questions.items():
        assert spec["type"] in {"choice", "noul"}
        assert spec["instructions"]
        if spec["type"] == "choice":
            assert len(spec["criteria"]) <= MAX_OPTIONS
    assert "none" in questions["proj_section_role"]["criteria"]
    assert set(questions["proj_music_under_talk"]["criteria"]) == {"none", "bed"}
    assert questions["proj_music_under_talk"]["criteria"]["none"] == "only the camera audio"


def test_prior_answers(profile):
    opening = profile.prior("cold_open_kind")
    assert opening.value == "talking_hook"
    assert opening.confidence == pytest.approx(0.667)
    slam = profile.prior("slam_line")
    assert slam.value == pytest.approx(0.02)
    assert slam.probabilities == {"true": 0.02, "false": 0.98}
    with pytest.raises(KeyError):
        profile.prior("nope")


def test_taste_prefs_are_valid_conductor_taste(profile):
    prefs = profile.taste_prefs()
    _validate_prefs(prefs)
    assert prefs["target_pace"] == "loose"
    assert prefs["cold_open_bias"] == "keep"


def test_value_and_param_lookup(profile):
    assert profile.value("typography.narration_caption.casing") == "lowercase_keep_proper_nouns"
    assert profile.value("no.such.param", default=None) is None
    with pytest.raises(KeyError):
        profile.value("no.such.param")
    with pytest.raises(KeyError):
        profile.param("typography")


def test_below_lists_soft_params(profile):
    soft = profile.below(0.3)
    assert "rhythm.speech_over_cuts.jl_cuts" in soft
    assert "graphics.chapter_card.hold_s" not in soft


def test_minimal_profile_is_valid():
    assert validate_profile(_minimal()) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(schema="other/1"), "schema must be"),
        (lambda d: d.update(sources=[]), "sources must be"),
        (lambda d: d["sources"][0].update(weight=2), "weight must be"),
        (lambda d: d["params"]["rhythm"]["montage"].update(bare=3), "bare value"),
        (lambda d: d["params"]["rhythm"]["montage"]["shot_median_s"].update(confidence=1.5), "confidence must be"),
        (lambda d: d["params"]["rhythm"]["montage"]["shot_median_s"].pop("evidence"), "evidence must be"),
        (lambda d: d["params"]["rhythm"]["montage"]["shot_median_s"].update(extra=1), "unknown keys"),
        (lambda d: d["params"]["rhythm"]["montage"]["shot_median_s"].update(decided_by="ai"), "decided_by must be"),
        (lambda d: d["params"]["rhythm"]["montage"]["cut_to_beat"].pop("question"), "decided_by jev needs a question"),
        (lambda d: d["params"]["rhythm"].update(Bad={"value": 1, "confidence": 1, "evidence": "x"}), "lower_snake_case"),
        (lambda d: d["params"]["rhythm"].update(empty={}), "empty group"),
        (lambda d: d["runtime_questions"][0].update(kind="score"), "kind must be"),
        (lambda d: d["runtime_questions"][0].update(prior=1.2), "noul prior"),
        (lambda d: d["runtime_questions"][0].update(sets="rhythm.nope"), "sets names no param"),
        (lambda d: d["runtime_questions"][0].pop("applies_to"), "applies_to"),
        (lambda d: d["runtime_questions"].append(copy.deepcopy(d["runtime_questions"][0])), "duplicate id"),
        (
            lambda d: d["runtime_questions"].append(
                {"id": "pick", "kind": "choice", "applies_to": "x", "instructions": "x", "options": {"a": "a", "b": "b"}, "prior": {"a": 0.5, "b": 0.2}}
            ),
            "sum to 1",
        ),
        (
            lambda d: d["runtime_questions"].append(
                {"id": "pick", "kind": "choice", "applies_to": "x", "instructions": "x", "options": {f"o{i}": "x" for i in range(MAX_OPTIONS)}, "prior": {"o0": 1.0}}
            ),
            "MAX_OPTIONS",
        ),
        (
            lambda d: d["params"].update(conductor_taste={"target_pace": {"value": "frantic", "confidence": 0.5, "evidence": "x"}}),
            "target_pace must be",
        ),
    ],
)
def test_validation_catches(mutate, message):
    data = _minimal()
    mutate(data)
    errors = validate_profile(data)
    assert any(message in error for error in errors), errors


def test_load_by_path_and_env(tmp_path, monkeypatch):
    style_dir = tmp_path / "tiny"
    style_dir.mkdir()
    (style_dir / "profile.json").write_text(json.dumps(_minimal()))
    assert load_profile(style_dir / "profile.json").name == "tiny"
    assert load_profile("tiny", styles_dir=tmp_path).name == "tiny"
    monkeypatch.setenv("JEVID_STYLES_DIR", str(tmp_path))
    assert load_profile("tiny").name == "tiny"


def test_load_errors(tmp_path):
    with pytest.raises(ConductorError, match="no style profile"):
        load_profile("missing", styles_dir=tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConductorError, match="not JSON"):
        load_profile(bad)
    invalid = tmp_path / "invalid.json"
    data = _minimal()
    data["params"]["rhythm"]["montage"]["shot_median_s"]["confidence"] = 7
    invalid.write_text(json.dumps(data))
    with pytest.raises(ConductorError, match="confidence must be"):
        load_profile(invalid)


def test_measured_mismatches_flags_drift(tmp_path):
    data = _minimal()
    data["params"]["rhythm"]["montage"]["shot_median_s"]["measured"] = "rhythm.median"
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data))
    prof = load_profile(path)
    assert measured_mismatches(prof, {"rhythm": {"median": 2.05}}) == []
    assert "measured 3.0" in measured_mismatches(prof, {"rhythm": {"median": 3.0}})[0]
    assert "not in measured.json" in measured_mismatches(prof, {})[0]
    data["params"]["rhythm"]["montage"]["shot_median_s"]["adjusted"] = "rounded for the engine"
    path.write_text(json.dumps(data))
    assert measured_mismatches(load_profile(path), {"rhythm": {"median": 3.0}}) == []


def test_section_labels_tile_each_video(measured):
    labels = json.loads((STUDY / "sections.json").read_text())
    kinds = set(labels["section_types"])
    for vid, video in labels["videos"].items():
        sections = video["sections"]
        assert sections[0]["start"] == 0.0, vid
        for left, right in zip(sections, sections[1:]):
            assert left["end"] == right["start"], (vid, left, right)
        assert sections[-1]["end"] == pytest.approx(measured["videos"][vid]["duration_s"], abs=0.01), vid
        assert {s["type"] for s in sections} <= kinds, vid


def test_analyze_needs_a_bundle(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("byjustinwu_analyze", STUDY / "analyze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--bundle", str(tmp_path)]) == 2
    assert "no data/ folder" in capsys.readouterr().err


def test_style_folder_stays_small():
    files = [p for p in STUDY.parent.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert not [p for p in files if p.suffix.lower() in {".zip", ".mp4", ".mov", ".mkv", ".webm"}]
    images = [p for p in files if p.suffix.lower() in {".jpg", ".png"}]
    assert all(p.parent.name == "reference" for p in images)
    assert sum(p.stat().st_size for p in images) < 150_000

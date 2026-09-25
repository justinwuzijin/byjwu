"""The byjustinwu profile: engine schema, evidence for every value, tied to its study.

The profile follows the assembly engine's ``jevid.style`` schema. When that
engine's loader is importable (``conductor.style.load_style``), the profile
is also loaded and validated through it, ``extends: base`` included.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from cutmcp.jev import MAX_OPTIONS, choice, noul

ROOT = Path(__file__).resolve().parents[1]
STYLE = ROOT / "styles" / "byjustinwu"
STUDY = STYLE / "study"
META = frozenset(
    {"schema", "schema_version", "name", "extends", "version", "provisional", "description", "provenance", "assumptions", "decisions"}
)
SECTIONS = ("intro", "title_card", "talking", "montage", "outro", "end_card")
HEX = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
MISSING = object()
TOLERANCE = 0.05


@pytest.fixture(scope="module")
def profile() -> dict:
    return json.loads((STYLE / "profile.json").read_text())


@pytest.fixture(scope="module")
def measured() -> dict:
    return json.loads((STUDY / "measured.json").read_text())


@pytest.fixture(scope="module")
def evidence(profile) -> dict:
    return profile["provenance"]["evidence"]


def _lookup(tree, dotted: str):
    node = tree
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return MISSING
    return node


def _leaves(node, prefix=""):
    for key, child in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if not prefix and key in META:
            continue
        if isinstance(child, dict) and child:
            yield from _leaves(child, path)
        else:
            yield path


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def test_header_matches_the_engine_schema(profile):
    assert profile["schema"] == "jevid.style"
    assert profile["schema_version"] == 1
    assert profile["name"] == "byjustinwu"
    assert profile["extends"] == "base"
    assert profile["provisional"] is False
    assert profile["provenance"]["study"] == "styles/byjustinwu/study"


def test_every_value_has_evidence(profile, evidence):
    keys = set(evidence)
    missing = []
    for leaf in _leaves(profile):
        parts = leaf.split(".")
        if not any(".".join(parts[:n]) in keys for n in range(2, len(parts) + 1)):
            missing.append(leaf)
    assert missing == []


def test_evidence_entries_are_well_formed(profile, evidence):
    for key, entry in evidence.items():
        assert _lookup(profile, key) is not MISSING, key
        assert set(entry) <= {"confidence", "evidence", "measured", "adjusted"}, key
        assert _number(entry["confidence"]) and 0.0 <= entry["confidence"] <= 1.0, key
        assert isinstance(entry["evidence"], str) and entry["evidence"].strip(), key


def test_values_match_the_study(profile, evidence, measured):
    problems = []
    for key, entry in evidence.items():
        pointer = entry.get("measured")
        if not pointer:
            continue
        found = _lookup(measured, pointer)
        if found is MISSING:
            problems.append(f"{key}: {pointer} not in measured.json")
            continue
        if "adjusted" in entry:
            continue
        value = _lookup(profile, key)
        if _number(value) and _number(found):
            if abs(value - found) > TOLERANCE * max(1.0, abs(found)):
                problems.append(f"{key}: {value} but measured {found}")
        elif value != found:
            problems.append(f"{key}: {value!r} but measured {found!r}")
    assert problems == []


def test_headline_numbers(profile, measured):
    assert profile["background"]["card_hold_seconds"] == measured["cards"]["chapter_card_hold_s"]["median"] == 5.714
    assert profile["pacing"]["montage"]["fast"]["shot_length"]["median"] == measured["rhythm"]["by_section"]["montage"]["thr0p3"]["median"]
    assert profile["music"]["sections"]["talking"]["bed_db"] == -96
    assert profile["typography"]["subtitle"]["enabled"] is False


def test_engine_constraints_hold(profile):
    for name in profile["structure"]["order"]:
        assert name in SECTIONS
    assert "talking" in profile["structure"]["order"]
    for name, spec in profile["pacing"].items():
        shot = spec["shot_length"]
        assert shot["p10"] <= shot["median"] <= shot["p90"], name
        assert spec["cut_on_beat"] in {"off", "prefer", "always"}, name
    assert set(profile["music"]["sections"]) <= set(SECTIONS)
    assert set(profile["background"]["sections"]) <= set(SECTIONS)
    assert all(HEX.match(c) for c in profile["background"]["palette"])
    assert HEX.match(profile["background"]["base_color"])
    for which in ("title", "subtitle"):
        spec = profile["typography"][which]
        assert HEX.match(spec["color"])
        assert all(-1 <= x <= 1 for x in spec["position"])
        assert 0.005 <= spec["size"] <= 1
    for name in profile["typography"]["title"]["treatments"]:
        assert name == "none" or name in profile["text_treatments"]


def test_decisions_are_askable(profile):
    seen = set()
    for spec in profile["decisions"]:
        qid = spec["id"]
        assert re.match(r"^[a-z][a-z0-9_]*$", qid) and qid not in seen
        seen.add(qid)
        assert isinstance(spec["editorial"], bool), qid
        assert spec["applies_to"].strip() and spec["instructions"].strip() and spec["evidence"].strip(), qid
        assert _lookup(profile, spec["sets"]) is not MISSING, qid
        if spec["kind"] == "choice":
            built = choice(spec["instructions"], spec["options"], add_none=spec.get("add_none", True))
            assert 2 <= len(built["criteria"]) <= MAX_OPTIONS, qid
            assert set(spec["prior"]) <= set(spec["options"]), qid
            assert sum(spec["prior"].values()) == pytest.approx(1.0, abs=0.01), qid
        else:
            assert spec["kind"] == "noul", qid
            built = noul(spec["instructions"], spec.get("true"), spec.get("false"))
            assert 0.0 <= spec["prior"] <= 1.0, qid
        assert built["instructions"] == spec["instructions"]
    linear = {s["id"] for s in profile["decisions"] if not s["editorial"]}
    assert {"section_role", "music_under_talk", "montage_beat_lock", "keep_pause"} <= linear


def test_engine_loader_accepts_it_when_present():
    style = pytest.importorskip("conductor.style")
    if not hasattr(style, "load_style") or not (ROOT / "styles" / "base" / "profile.json").is_file():
        pytest.skip("the assembly engine's style loader is not on this branch")
    loaded = style.load_style("byjustinwu")
    assert loaded.version == "1.0.0" and not loaded.provisional
    assert all("decisions" in w for w in loaded.warnings)


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
    files = [p for p in STYLE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert not [p for p in files if p.suffix.lower() in {".zip", ".mp4", ".mov", ".mkv", ".webm"}]
    images = [p for p in files if p.suffix.lower() in {".jpg", ".png"}]
    assert all(p.parent.name == "reference" for p in images)
    assert sum(p.stat().st_size for p in images) < 150_000

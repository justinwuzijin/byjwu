"""Bad edits of the synthetic shoot must score well below a real assembly.

The scorer reads the FCPXML (and, for hook and hygiene, an assembly report).
These edits are built by hand from the same media. They do not go through
the assembler, so a high score here would mean the number is not about the cut.
"""

from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generate = _load("synth_e2e_shoot_bad", ROOT / "scripts" / "synth_e2e_shoot.py").generate
score_timeline = _load("style_score_bad", ROOT / "eval" / "style_score.py").score_timeline

pytestmark = pytest.mark.slow


def _time(value: Fraction) -> str:
    from conductor.timeutil import format_time

    return format_time(value)


def _starter(folder: Path):
    from conductor.fcpxml import write_document
    from conductor.ingest import inventory, render_starter

    found = inventory(folder)
    tree = render_starter(found.clips, name="bad")
    return found, tree, write_document


def _spine(tree: ET.ElementTree) -> ET.Element:
    spine = tree.getroot().find(".//spine")
    assert spine is not None
    return spine


def _chapters(clip: ET.Element, marks: list[tuple[Fraction, str]]) -> None:
    frame = Fraction(1, 24)
    for at, kind in marks:
        ET.SubElement(
            clip,
            "chapter-marker",
            {
                "start": _time(at),
                "duration": _time(frame),
                "value": kind,
                "note": f"jevid.section={kind}",
                "posterOffset": "0s",
            },
        )


def _add_music(tree: ET.ElementTree, song: Path, starts: list[Fraction]) -> None:
    """Place the bed at full volume, each copy starting at the file head."""
    root = tree.getroot()
    resources = root.find("resources")
    assert resources is not None
    used = [int(el.get("id")[1:]) for el in resources if el.get("id", "").startswith("r")]
    ident = f"r{max(used) + 1}"
    asset = ET.SubElement(
        resources,
        "asset",
        {
            "id": ident,
            "name": song.stem,
            "start": "0s",
            "duration": "48s",
            "hasAudio": "1",
            "audioSources": "1",
            "audioChannels": "1",
            "audioRate": "48000",
            "format": "r1",
        },
    )
    ET.SubElement(asset, "media-rep", {"kind": "original-media", "src": song.resolve().as_uri()})
    host = _spine(tree)[0]
    for start in starts:
        ET.SubElement(
            host,
            "asset-clip",
            {
                "ref": ident,
                "lane": "-1",
                "offset": _time(start),
                "name": song.stem,
                "start": "0s",
                "duration": "20s",
                "audioRole": "music",
            },
        )


def _split_spine(tree: ET.ElementTree) -> None:
    """Replace each whole clip with short pieces that ignore the beat."""
    spine = _spine(tree)
    clips = list(spine)
    for child in clips:
        spine.remove(child)
    cursor = Fraction(0)
    piece = Fraction(3, 4)
    for child in clips:
        duration = Fraction(child.get("duration").replace("s", ""))
        local = Fraction(0)
        while local + piece < duration:
            copy = ET.fromstring(ET.tostring(child))
            copy.set("offset", _time(cursor))
            copy.set("start", _time(local))
            copy.set("duration", _time(piece))
            spine.append(copy)
            cursor += piece
            local += piece


@pytest.fixture(scope="module")
def shoot(tmp_path_factory):
    folder = tmp_path_factory.mktemp("shoot")
    info = generate(folder, sidecars=True)
    assert info["clips"] >= 8
    return folder


def test_stringout_and_random_cuts_score_at_most_60(shoot, tmp_path):
    _found, tree, write = _starter(shoot)
    stringout = tmp_path / "stringout.fcpxml"
    write(tree, stringout)
    plain = score_timeline(stringout, style="byjustinwu")
    assert plain["overall"] <= 60, plain["dimensions"]

    _found, random_tree, write = _starter(shoot)
    _split_spine(random_tree)
    first = _spine(random_tree)[0]
    _chapters(first, [(Fraction(0), "intro"), (Fraction(0), "talking")])
    random_path = tmp_path / "random.fcpxml"
    write(random_tree, random_path)
    random = score_timeline(random_path, style="byjustinwu")
    assert random["overall"] <= 60, random["dimensions"]
    assert random["overall"] < 80


def test_dead_air_retakes_and_unducked_music_score_lower(shoot, tmp_path):
    _found, tree, write = _starter(shoot)
    song = shoot / "bed_120bpm.wav"
    assert song.is_file()
    _add_music(tree, song, [Fraction(0), Fraction(20)])
    path = tmp_path / "messy.fcpxml"
    write(tree, path)
    payload = {
        "units": [
            {"id": "u1", "kind": "speech", "text": "Um."},
            {"id": "u2", "kind": "speech", "text": "So today we are"},
            {"id": "u3", "kind": "speech", "text": "So today we are going to fix why every edit I make feels slow."},
        ],
        "decisions": [
            {"key": "u1_keep", "value": 0.9, "item": "u1"},
            {"key": "u2_keep", "value": 0.9, "item": "u2"},
            {"key": "u3_keep", "value": 0.9, "item": "u3"},
            {"key": "u2_section", "value": "intro", "item": "u2"},
        ],
    }
    report_path = tmp_path / "assembly.json"
    report_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    report = score_timeline(path, style="byjustinwu", assembly=report_path)
    assert report["overall"] <= 60, report["dimensions"]
    assert report["dimensions"]["ducking"] <= 40
    assert report["dimensions"]["hygiene"] <= 70
    assert report["dimensions"]["hook"] < 50

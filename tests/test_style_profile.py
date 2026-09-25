"""Style profiles: loading, inheritance, validation."""

from __future__ import annotations

import json

import pytest

from conductor.errors import ConductorError
from conductor.style import (
    SCHEMA,
    available_styles,
    hex_rgba,
    load_style,
    style_from_dict,
)


def test_byjustinwu_is_measured_and_extends_base():
    profile = load_style("byjustinwu")
    assert profile.provisional is False
    assert profile.version == "1.0.0"
    assert profile.chain == ("byjustinwu", "base")
    assert profile.get("typography.title.font") == "SF Pro Display"
    assert profile.get("typography.subtitle.font") == "SF Pro Text"
    assert profile.get("background.enabled") is True
    assert profile.get("music.duck.depth_db") < 0
    assert profile.get("assumptions")
    # inherited from base, not restated
    assert profile.get("music.floor_db") == -96
    assert profile.warnings == ("unknown top-level key 'decisions' kept but not read",)


def test_default_style_is_byjustinwu_and_both_ship():
    assert load_style().name == "byjustinwu"
    assert {"base", "byjustinwu"} <= set(available_styles())


def test_every_treatment_the_profile_names_exists():
    profile = load_style("byjustinwu")
    treatments = profile.get("text_treatments")
    for name in profile.get("typography.title.treatments"):
        assert name in treatments
    for name in profile.get("typography.subtitle.emphasis.treatments"):
        assert name in treatments


def test_summary_is_numbers_for_the_decision_state():
    summary = load_style("byjustinwu").summary()
    assert summary["asl_seconds"]["montage"] < summary["asl_seconds"]["talking"]
    assert summary["cut_on_beat"]["montage"] == "prefer"


def test_extends_resolves_in_a_custom_folder(tmp_path, monkeypatch):
    folder = tmp_path / "styles" / "tight"
    folder.mkdir(parents=True)
    (folder / "profile.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "schema_version": 1,
                "name": "tight",
                "extends": "byjustinwu",
                "version": "0.0.1",
                "provisional": False,
                "pacing": {"montage": {"asl_seconds": 0.5}},
            }
        )
    )
    monkeypatch.setenv("JEVID_STYLES_DIR", str(tmp_path / "styles"))
    profile = load_style("tight")
    assert profile.chain == ("tight", "byjustinwu", "base")
    assert profile.pacing("montage")["asl_seconds"] == 0.5
    assert profile.pacing("montage")["cut_on_beat"] == "prefer"
    assert load_style(folder).name == "tight"
    assert load_style(folder / "profile.json").name == "tight"


def test_yaml_profiles_load_when_pyyaml_is_installed(tmp_path):
    yaml = pytest.importorskip("yaml")
    folder = tmp_path / "yamlstyle"
    folder.mkdir()
    (folder / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": SCHEMA,
                "schema_version": 1,
                "name": "yamlstyle",
                "extends": "base",
                "version": "1",
                "provisional": False,
                "music": {"bed_db": -9},
            }
        )
    )
    assert load_style(folder).get("music.bed_db") == -9


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"schema_version": 2}, "newer than this engine"),
        ({"pacing": {"montage": {"cut_on_beat": "sometimes"}}}, "cut_on_beat"),
        ({"typography": {"title": {"treatments": ["melt"]}}}, "unknown treatment 'melt'"),
        ({"background": {"base_color": "black"}}, "base_color"),
        ({"structure": {"order": ["intro", "montage"]}}, "at least one 'talking'"),
        ({"music": {"duck": {"depth_db": 6}}}, "depth_db"),
        ({"text_treatments": {"none": {}, "bad": {"scale": [[0, [1]]]}}}, "keyframe"),
        ({"background": {"lane": 0}}, "background.lane"),
    ],
)
def test_invalid_values_are_refused(override, message):
    data = {"name": "broken", "version": "0", "provisional": False, "schema": SCHEMA, "schema_version": 1}
    data.update(override)
    with pytest.raises(ConductorError, match=message):
        style_from_dict(data)


def test_unknown_top_level_keys_are_kept_as_warnings():
    profile = style_from_dict(
        {"name": "x", "version": "0", "schema": SCHEMA, "schema_version": 1, "colour_grade": {"lut": "a"}}
    )
    assert profile.get("colour_grade.lut") == "a"
    assert any("colour_grade" in w for w in profile.warnings)


def test_unknown_style_name_lists_the_known_ones():
    with pytest.raises(ConductorError, match="Known: .*byjustinwu"):
        load_style("nobody")


def test_hex_colours():
    assert hex_rgba("#FF0000") == (1.0, 0.0, 0.0, 1.0)
    assert hex_rgba("#00000080")[3] == pytest.approx(128 / 255)

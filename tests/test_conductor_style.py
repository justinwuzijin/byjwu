"""Style profiles load, validate, and stay tied to the study measurements."""

from __future__ import annotations

import copy
import csv
import importlib.util
import json
from pathlib import Path

import pytest

from conductor.errors import ConductorError
from conductor.style import SCHEMA, load_profile, resolve, validate

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "styles" / "byjustinwu" / "profile.json"
STUDY_DATA = ROOT / "styles" / "byjustinwu" / "study" / "data"
MEASUREMENTS = STUDY_DATA / "measurements.json"
MOTION = STUDY_DATA / "motion.json"
ANALYZE = ROOT / "styles" / "byjustinwu" / "study" / "analyze.py"


def _minimal() -> dict:
    return {
        "schema": SCHEMA,
        "name": "doc",
        "sources": [{"id": "abc", "weight": 1.0}],
        "params": {
            "cut_rhythm": {
                "shot_s": {"value": 3.0, "range": [1.0, 5.0], "confidence": 0.5, "evidence": "abc 12s"},
            }
        },
        "jev_questions": [
            {"key": "role", "primitive": "choice", "instructions": "which role?",
             "options": {"a": "first", "b": "second"}, "feeds": "cut_rhythm.shot_s"},
            {"key": "pick", "primitive": "choice", "instructions": "which line?",
             "options_from": "transcript_lines"},
            {"key": "brk", "primitive": "noul", "instructions": "a break here?"},
        ],
    }


def test_byjustinwu_profile_is_valid_and_every_value_has_evidence():
    data = json.loads(PROFILE.read_text())
    assert validate(data) == []
    profile = load_profile("byjustinwu")
    params = profile.params()
    assert len(params) > 50
    for param in params:
        assert 0 <= param.confidence <= 1
        assert param.evidence.strip()
    assert {p.decided_by for p in params} == {"profile", "jev", "taste_model"}


@pytest.mark.parametrize("ref", ["byjustinwu", "styles/byjustinwu/profile", str(PROFILE), str(PROFILE.parent)])
def test_resolve_accepts_name_taste_ref_and_paths(ref):
    assert resolve(ref) == PROFILE


def test_unknown_profile_is_a_conductor_error():
    with pytest.raises(ConductorError, match="no style profile"):
        load_profile("nobody")


def test_param_lookup_and_defaults():
    profile = load_profile("styles/byjustinwu/profile")
    assert profile.value("music.card_silence") is True
    assert profile.param("cut_rhythm.talking.hold_median_s").unit == "s"
    assert profile.value("cut_rhythm.nope", default=None) is None
    with pytest.raises(ConductorError, match="no param"):
        profile.value("cut_rhythm.nope")
    with pytest.raises(ConductorError, match="is a group"):
        profile.param("cut_rhythm.talking")


@pytest.mark.skipif(not MEASUREMENTS.exists(), reason="study data is local only; run study/analyze.py")
def test_profile_numbers_match_the_study_measurements():
    m = json.loads(MEASUREMENTS.read_text())
    profile = load_profile("byjustinwu")
    pooled = m["cut_rhythm"]["pooled_long_form_shot_length_s_by_section"]
    assert profile.value("cut_rhythm.talking.hold_median_s") == pytest.approx(pooled["talking"]["median"], abs=0.05)
    assert profile.value("cut_rhythm.broll.shot_median_s") == pytest.approx(pooled["broll"]["median"], abs=0.05)
    low, high = profile.param("cut_rhythm.cuts_per_minute").range
    for row in m["cut_rhythm"]["per_video"].values():
        assert low - 0.05 <= row["cuts_per_minute_thr0p3"] <= high + 0.05
    cards = next(v["chapters"] for v in m["chapter_cards"].values()
                 if isinstance(v, dict) and sum(1 for c in v.get("chapters", [])[1:-1] if c.get("silent_during")) == 5)
    assert all("title" not in c for c in cards)
    silent = [c for c in cards[1:-1] if c["silent_during"]]
    assert len(silent) == 5
    frames = [s[2] for c in cards for s in c["shots_start_len_frames"]]
    assert frames.count(137) == 3
    assert m["beat_alignment"]["pooled"]["lift"] > 1.5


@pytest.mark.skipif(not MOTION.exists(), reason="excerpt data is local only; run study/motion.py")
def test_profile_motion_matches_the_excerpt_measurements():
    motion = json.loads(MOTION.read_text())["regions"]
    profile = load_profile("byjustinwu")
    rates = "background_layer.motion.texture_updates_per_s"
    assert profile.value(f"{rates}.glitch_and_line_art") == pytest.approx(
        next(v for k,v in motion.items() if k.endswith("glitch_rect"))["typical_updates_per_s"], abs=0.5)
    assert next(v for k,v in motion.items() if k.endswith("title_card_art"))["typical_hold_frames"] == 1
    gradient = next(v for k,v in motion.items() if k.endswith("date_stamp_texture"))
    assert gradient["fps"] == 30.0
    low, high = profile.param(f"{rates}.liquid_gradient").range
    assert low <= gradient["typical_updates_per_s"] <= high


def test_jev_questions_use_cutmcp_constructors():
    profile = load_profile("byjustinwu")
    questions = profile.jev_questions()
    assert "cold_open_line" not in questions
    assert questions["section_role"]["type"] == "choice"
    assert "none" not in questions["section_role"]["criteria"]
    assert questions["chapter_break"]["type"] == "noul"
    filled = profile.jev_questions({"cold_open_line": {"c1@3.5": "so, the date is"}})
    assert set(filled["cold_open_line"]["criteria"]) == {"c1@3.5", "none"}


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(schema="other/1"), "schema must be"),
        (lambda d: d["sources"].clear(), "sources must be"),
        (lambda d: d["sources"][0].update(weight=2), "weight must be"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].pop("confidence"), "confidence must be"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].update(confidence=1.5), "confidence must be"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].update(confidence=True), "confidence must be"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].update(evidence=" "), "evidence must be"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].update(decided_by="grok"), "decided_by must be"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].update(value=9.0), "outside its range"),
        (lambda d: d["params"]["cut_rhythm"]["shot_s"].update(range=[5, 1]), "range must be"),
        (lambda d: d["params"].update(empty={}), "non-empty group"),
        (lambda d: d["jev_questions"][0].update(primitive="score"), "primitive must be"),
        (lambda d: d["jev_questions"][0].pop("options"), "options must be"),
        (lambda d: d["jev_questions"][0].update(options={str(i): "x" for i in range(250)}), "more than 250"),
        (lambda d: d["jev_questions"][0].update(feeds="cut_rhythm.missing"), "unknown param"),
        (lambda d: d.update(unknowns=[{"topic": "fonts"}]), "topic and a why"),
    ],
)
def test_validate_reports_each_problem(mutate, message):
    data = copy.deepcopy(_minimal())
    assert validate(data) == []
    mutate(data)
    errors = validate(data)
    assert any(message in e for e in errors), errors


def test_invalid_profile_file_raises_with_all_errors(tmp_path):
    bad = _minimal()
    bad["params"]["cut_rhythm"]["shot_s"]["confidence"] = 7
    bad["params"]["cut_rhythm"]["shot_s"]["evidence"] = ""
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "profile.json").write_text(json.dumps(bad))
    with pytest.raises(ConductorError) as exc:
        load_profile("doc", styles_dir=tmp_path)
    assert "confidence" in str(exc.value) and "evidence" in str(exc.value)


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _fake_video(root: Path, vid: str) -> None:
    """150 s: talking 0-40 s, music with 1 s cuts 40-80 s, talking to 90 s,
    a digitally silent card 90-94 s, then talking to the end."""
    d = root / "data" / vid
    d.mkdir(parents=True)
    duration = 150.0

    def zone(t: float) -> str:
        if 40 <= t < 80:
            return "music"
        if 90 <= t < 94:
            return "card"
        return "talk"

    (d / "summary.json").write_text(json.dumps({
        "duration_s": duration,
        "shots": {"0p3": {"cuts_per_minute": 20.0}, "0p15": {"cuts_per_minute": 21.0}},
        "loudness_summary": {"integrated_lufs": -12.0, "lra_lu": 10.0, "true_peak_dbfs": -1.0},
    }))
    (d / "metadata.json").write_text(json.dumps({"chapters": [
        {"start_time": 0, "title": "one"}, {"start_time": 90, "title": "two"}]}))
    (d / "ffprobe.json").write_text(json.dumps({"streams": [
        {"codec_type": "video", "avg_frame_rate": "24000/1001"}]}))
    starts = [0.0, 10.0, 20.0, 30.0] + [40.0 + i for i in range(40)] + [80.0, 90.0, 94.0, 110.0, 130.0]
    shots = [[i, s, e, e - s, 0.5] for i, (s, e) in enumerate(zip(starts, starts[1:] + [duration]))]
    header = ["shot_index", "start_s", "end_s", "length_s", "cut_scene_score"]
    _write_csv(d / "shots_thr0p3.csv", header, shots)
    _write_csv(d / "shots_thr0p15.csv", header, shots)
    level = {"talk": -24.0, "music": -12.0, "card": -139.0}
    loud = []
    for k in range(1, int(duration * 10) + 1):
        t = k / 10
        m = level[zone(t)] + (3.0 if zone(t) == "music" and abs(t - round(t)) < 0.05 else 0.0)
        loud.append([t, m, "nan" if zone(t) == "card" else m, -12.0, 10.0])
    _write_csv(d / "loudness_ebur128_100ms.csv",
               ["time_s", "momentary_lufs_400ms", "shortterm_lufs_3s", "integrated_lufs_so_far", "lra_lu_so_far"], loud)
    mid = {"talk": -28.0, "music": -14.0, "card": -80.0}
    spec = [[t / 2, mid[zone(t / 2)], -12.0, 500, 0.01, 0.05, 0.3, 0.2, 0.2, 0.2, 0.2, 0.2]
            for t in range(int(duration * 2))]
    _write_csv(d / "spectral_features_500ms.csv",
               ["time_s", "rms_dbfs_mid", "side_to_mid_db", "spectral_centroid_hz", "spectral_flatness",
                "zero_crossing_rate", "energy_cv_50ms_subwindows", "band_0_150hz", "band_150_500hz",
                "band_500_2000hz", "band_2000_4000hz", "band_4000_8000hz"], spec)
    times = [round(0.3 + i * 0.35, 2) for i in range(int(duration / 0.35))]
    times = [t for t in times if zone(t) == "talk" and zone(t + 0.8) == "talk"]
    words = [[t, "word", n] for t, n in zip(times, times[1:] + [duration])]
    _write_csv(d / "transcript_words.csv", ["start_s", "word", "next_word_start_s"], words)
    (d / "silence_map.json").write_text(json.dumps({"silences": {
        "-40dB_0.5s": [{"start_s": 90.2, "end_s": 94.0, "duration_s": 3.8}], "-30dB_0.3s": []}}))


def test_analysis_script_runs_on_a_bundle(tmp_path):
    spec = importlib.util.spec_from_file_location("byjustinwu_analyze", ANALYZE)
    analyze = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analyze)
    _fake_video(tmp_path, "sample")
    out = tmp_path / "m.json"
    assert analyze.main([str(tmp_path), "--out", str(out)]) == 0
    m = json.loads(out.read_text())
    vid = "sample"
    by = m["cut_rhythm"]["per_video"][vid]["shot_length_s_by_section"]
    assert by["montage"]["median"] == pytest.approx(1.0)
    assert m["chapter_cards"][vid]["chapters"][1]["silent_during"]
    assert m["music_under_speech"][vid]["music_minus_speech_mid_db"] == pytest.approx(14.0)
    assert m["beat_alignment"]["pooled"]["hit_rate"] > m["beat_alignment"]["pooled"]["chance_rate"]

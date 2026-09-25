"""End-to-end assembly on synthetic footage, validated against the FCPXML DTD."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction

import httpx
import pytest

from conductor.assembly import assemble
from conductor.assembly.synth import make_fixture
from conductor.cli import main
from conductor.errors import ConductorError
from conductor.ingest import file_hash
from conductor.style import load_style, style_from_dict
from conductor.timeutil import parse_time
from conductor.validate import dtd_available, validate_fcpxml

BRIEF = "A 40-second test of the jevid assembly engine"
needs_lxml = pytest.mark.skipif(not dtd_available(), reason="lxml is not installed")


@pytest.fixture(scope="module")
def shoot(tmp_path_factory):
    folder = tmp_path_factory.mktemp("shoot")
    make_fixture(folder, use_ffmpeg=False)
    return folder


@pytest.fixture(scope="module")
def small_style():
    """Engine fixture. The shipped profile is the measured study; these tests
    need the shorter provisional timings so a 40s synthetic brief still
    exercises fades, subtitles, punch-ins, and beat snapping.
    """
    base = load_style("byjustinwu").data
    data = {
        **base,
        "name": "byjustinwu-test",
        "structure": {
            **base["structure"],
            "target_seconds": 480,
            "tolerance_seconds": 8,
            "sections": {
                "intro": {"share": 0.07, "min_seconds": 4, "max_seconds": 18},
                "talking": {"share": 0.74, "min_seconds": 5, "max_seconds": None},
                "montage": {"share": 0.13, "min_seconds": 4, "max_seconds": 35},
                "outro": {"share": 0.04, "min_seconds": 0, "max_seconds": 15},
            },
            "title_card": {"seconds": 2.0, "bars": 2, "text": "title"},
            "end_card": {"seconds": 3.0, "bars": 2, "text": "byjustinwu"},
            "intro_flash_share": 0.5,
            "speech_merge_gap_seconds": 0.3,
        },
        "pacing": {
            "tolerance": 0.25,
            "intro": {"asl_seconds": 1.2, "shot_length": {"median": 1.0, "p10": 0.5, "p90": 2.2}, "cut_on_beat": "always", "broll_cover": 0.0},
            "talking": {"asl_seconds": 3.8, "shot_length": {"median": 3.4, "p10": 1.6, "p90": 7.0}, "cut_on_beat": "prefer", "broll_cover": 0.25},
            "montage": {"asl_seconds": 0.8, "shot_length": {"median": 0.75, "p10": 0.4, "p90": 1.5}, "cut_on_beat": "always", "broll_cover": 0.0},
            "outro": {"asl_seconds": 2.5, "shot_length": {"median": 2.2, "p10": 1.0, "p90": 4.5}, "cut_on_beat": "prefer", "broll_cover": 0.0},
        },
        "cuts": {
            **base["cuts"],
            "punch_in": {"enabled": True, "scale": 1.14, "every": 2},
            "j_cut": {"probability": 1.0, "lead_seconds": 0.35},
            "l_cut": {"probability": 0.15, "tail_seconds": 0.45},
        },
        "music": {
            **base["music"],
            "fade_in": {"seconds": 1.5, "curve": "easeIn"},
            "fade_out": {"seconds": 3.0, "curve": "easeOut"},
            "duck": {**base["music"]["duck"], "depth_db": -14},
            "sections": {
                name: {"bed_db": base["music"].get("bed_db", -10), "duck": name in {"intro", "talking", "outro"}}
                for name in ("intro", "title_card", "talking", "montage", "outro", "end_card")
            },
        },
        "typography": {
            **base["typography"],
            "subtitle": {
                **base["typography"]["subtitle"],
                "enabled": True,
                "font": "SF Pro Text",
                "face": "Semibold",
                "size": 0.042,
                "position": [0.0, -0.33],
                "case": "lower",
                "max_chars_per_line": 28,
            },
        },
        "background": {
            **base["background"],
            "render_scale": 0.2,
            "opacity": 0.9,
            "sections": {**base["background"].get("sections", {}), "intro": True, "end_card": True},
            "aroll_inset": {"intro": 0.8, "talking": 1.0, "montage": 1.0, "outro": 0.8},
        },
    }
    return style_from_dict(data, base=None)


@pytest.fixture(scope="module")
def result(shoot, small_style, tmp_path_factory):
    out = tmp_path_factory.mktemp("assembled")
    return assemble(
        media=shoot,
        brief=BRIEF,
        style=small_style,
        durations_path=shoot / "durations.json",
        out_dir=out,
    )


def _root(result):
    return ET.parse(result.fcpxml).getroot()


@needs_lxml
def test_output_is_dtd_valid(result):
    assert result.dtd_errors == []
    assert result.payload["dtd"] == {"checked": True, "valid": True, "errors": [], "version": "1.11"}
    assert validate_fcpxml(result.fcpxml) == []


def test_structure_follows_the_profile_order(result):
    kinds = [row["kind"] for row in result.payload["structure"]]
    assert kinds[0] == "intro" and kinds[-1] == "end_card"
    assert "title_card" in kinds and "montage" in kinds and kinds.count("talking") >= 1
    assert result.mode == "dry-run"
    total = result.payload["timeline"]["duration_seconds"]
    assert 20 <= total <= 48
    chapters = [el for el in _root(result).iter("chapter-marker")]
    assert len(chapters) == len(kinds)
    assert all("jevid.section=" in el.get("note") for el in chapters)


def test_music_has_fade_and_duck_keyframes(result):
    root = _root(result)
    music = [el for el in root.iter("asset-clip") if el.get("audioRole") == "music"]
    assert music, "no music bed"
    keys = [k for k in music[0].iter("keyframe")]
    values = [float(k.get("value")[:-2]) for k in keys]
    assert values[0] == -96 and values[-1] == -96, "fade from and to silence"
    start = parse_time(music[0].get("start"))
    fade_in = parse_time(keys[1].get("time")) - parse_time(keys[0].get("time"))
    fade_out = parse_time(keys[-1].get("time")) - parse_time(keys[-2].get("time"))
    assert fade_in == Fraction(3, 2) and fade_out == Fraction(3)
    assert parse_time(keys[0].get("time")) == start
    assert keys[0].get("interp") == "easeIn"
    ducked = [v for v in values if v <= -6 - 14 + 0.01]
    assert ducked, "music never ducked under dialogue"
    assert result.metrics["ducking"]["checked"] > 0
    assert result.metrics["ducking"]["compliant"] == result.metrics["ducking"]["checked"]
    assert result.metrics["music"]["compliant"] is True


def test_subtitles_are_sf_pro_titles_in_the_profile_style(result):
    root = _root(result)
    effect = next(root.iter("effect"))
    assert effect.get("name") == "Basic Title"
    subtitles = [t for t in root.iter("title") if (t.get("name") or "").startswith("Subtitle")]
    assert len(subtitles) >= 8
    style = subtitles[0].find("text-style-def/text-style")
    assert style.get("font") == "SF Pro Text" and style.get("fontFace") == "Semibold"
    assert float(style.get("fontSize")) == pytest.approx(0.042 * 1080, abs=0.1)
    text = subtitles[0].find("text/text-style").text
    assert text == text.lower() and len(text) <= 28
    position = subtitles[0].find("adjust-transform").get("position").split()
    assert float(position[1]) == pytest.approx(-0.33 * 1080, abs=0.5)
    assert result.metrics["subtitles"]["coverage"] >= 0.85
    assert result.metrics["fonts"]["compliance"] == 1.0


def test_emphasis_and_title_treatments_distort_text(result):
    root = _root(result)
    titles = [t for t in root.iter("title") if not (t.get("name") or "").startswith("Subtitle")]
    assert titles
    assert {c.find("text-style-def/text-style").get("font") for c in titles} == {"SF Pro Display"}
    scale_keys = [k for t in titles for p in t.iter("param") if p.get("name") == "scale" for k in p.iter("keyframe")]
    assert scale_keys, "title scale keyframes come from the style treatments"


def test_rectangle_background_layer_is_shapes(result):
    root = _root(result)
    shapes = [v for v in root.iter("video") if (v.get("name") or "").startswith("byjwu rectangle")]
    assert shapes, "rectangle layer is Shapes generator clips"
    lanes = {int(v.get("lane")) for v in shapes}
    assert all(lane < 0 for lane in lanes), "placement 'under' keeps the layer below the storyline"
    effect = next(el for el in root.iter("effect") if el.get("name") == "Shapes")
    assert shapes[0].get("ref") == effect.get("id")
    blends = [v.find("adjust-blend") for v in shapes if v.find("adjust-blend") is not None]
    assert blends and float(blends[0].get("amount")) == pytest.approx(0.9)
    pulses = [k for v in shapes for p in v.iter("param") if p.get("name") == "scale" for k in p.iter("keyframe")]
    assert pulses, "background does not move"
    insets = [c for c in root.iter("asset-clip") if (c.find("adjust-transform") is not None and c.find("adjust-transform").get("scale") == "0.8 0.8")]
    assert insets, "intro A-roll is not inset over the background"
    assert result.metrics["background"]["coverage"] >= 0.95


def test_markers_explain_every_choice(result):
    root = _root(result)
    markers = list(root.iter("marker"))
    values = [m.get("value") for m in markers]
    assert any(v.startswith("jevid · music") for v in values)
    assert any("speech" in v for v in values) and any("b-roll" in v for v in values)
    speech = next(m for m in markers if "speech" in m.get("value"))
    note = speech.get("note")
    assert "decision=u" in note and "rule=pacing." in note and "mock" in note
    music_note = next(m for m in markers if m.get("value").startswith("jevid · music")).get("note")
    assert "duck=-14" in music_note and "beats=120.0bpm" in music_note


def test_cuts_land_on_beats_in_always_sections(result):
    assert result.metrics["on_beat"]["beats_known"] is True
    assert result.metrics["on_beat"]["checked"] >= 2
    assert result.metrics["on_beat"]["ratio"] == 1.0


def test_split_edits_and_punch_ins_follow_the_profile(result):
    root = _root(result)
    spine = root.find(".//spine")
    split = [c for c in spine if c.get("audioStart") or c.get("audioDuration")]
    assert split, "no J/L cut in the talking sections"
    for clip in split:
        start = parse_time(clip.get("start"))
        audio_start = parse_time(clip.get("audioStart"), start)
        audio_duration = parse_time(clip.get("audioDuration"), parse_time(clip.get("duration")))
        assert audio_start <= start
        assert audio_duration >= parse_time(clip.get("duration"))
    punched = [c for c in spine if c.find("adjust-transform") is not None and c.find("adjust-transform").get("scale") == "1.14 1.14"]
    assert punched, "no punch-in on a jump cut"


def test_report_json_and_markdown(result):
    payload = json.loads(result.json.read_text())
    assert payload["protocol"] == "jevid.assembly" and payload["protocol_version"] == 1
    assert payload["style"]["provisional"] is False and payload["style"]["assumptions"]
    assert payload["decisions"] and all(d["source"].endswith(":mock") or d["source"] == "taste" for d in payload["decisions"])
    linear = [d for d in payload["decisions"] if d["lane"] == "linear"]
    creative = [d for d in payload["decisions"] if d["lane"] == "creative"]
    assert linear and all(d["source"].startswith("jev") or d["source"] == "taste" for d in linear)
    assert creative and all(d["source"].startswith("opus") or d["source"] == "taste" for d in creative)
    sources = {d["source"].split(":")[0] for d in payload["decisions"]}
    assert {"opus", "jev"} <= sources | {r["engine"] for r in payload["receipts"]}
    assert "select" in {r["stage"] for r in payload["receipts"]}
    assert "opus" in payload["decision_usage_summary"]
    text = result.markdown.read_text()
    assert "## Structure" in text and "What the style profile assumes" in text


def test_same_inputs_give_the_same_bytes(shoot, small_style, tmp_path, result):
    again = assemble(
        media=shoot,
        brief=BRIEF,
        style=small_style,
        durations_path=shoot / "durations.json",
        out_dir=tmp_path,
    )
    first = result.fcpxml.read_text().replace(str(result.fcpxml.parent), "OUT")
    second = again.fcpxml.read_text().replace(str(again.fcpxml.parent), "OUT")
    assert first == second


def test_source_media_is_never_written(shoot, small_style, tmp_path):
    before = {p.name: file_hash(p) for p in shoot.rglob("*") if p.is_file()}
    assemble(media=shoot, brief=BRIEF, style=small_style, durations_path=shoot / "durations.json", out_dir=tmp_path / "o")
    after = {p.name: file_hash(p) for p in shoot.rglob("*") if p.is_file()}
    assert before == after


def test_live_assembly_asks_claude_opus_for_every_editorial_call(shoot, small_style, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CONDUCTOR_TASTE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("CONDUCTOR_JEV_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("CONDUCTOR_DRY_RUN", raising=False)
    monkeypatch.delenv("CONDUCTOR_LLM_MODEL", raising=False)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), "model": body["model"]})
        if "decisions" in str(request.url):
            state = body["state"]
            answers = {}
            for key, spec in body["questions"].items():
                item_id = key[: -len("_pick")] if key.endswith("_pick") else key
                item = next((row for row in state["items"] if row["id"] == item_id), None)
                field = item_id.split("_", 1)[-1]
                choice = None
                if item and isinstance(item.get("heuristic"), dict) and field in item["heuristic"]:
                    choice = item["heuristic"][field]["value"]
                if choice is None:
                    criteria = (spec.get("criteria") or spec.get("options") or {}) if isinstance(spec, dict) else {}
                    choice = next(iter(criteria), "keep")
                answers[key] = {"choice": choice, "confidence": 0.9}
            return httpx.Response(200, json={"id": f"gen_{len(seen)}", "model": body["model"], "answers": answers})
        content = json.loads(body["messages"][0]["content"])
        decisions = []
        for item in content["items"]:
            hint = (item.get("subject") or {}).get("heuristic") or {}
            field = item["id"].split("_", 1)[-1]
            value = (hint.get(field) or {}).get("value")
            if value is None:
                options = item.get("options") or {}
                value = next(iter(options), "keep")
            if isinstance(value, float):
                value = "keep" if value >= 0.5 else "lose"
            decisions.append(
                {"id": item["id"], "value": value, "confidence": 0.91, "rationale": "live pick"}
            )
        return httpx.Response(
            200,
            json={
                "id": f"msg_{len(seen)}",
                "model": body["model"],
                "content": [{"type": "text", "text": json.dumps({"decisions": decisions})}],
                "stop_reason": "end_turn",
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    live = assemble(
        media=shoot,
        brief=BRIEF,
        style=small_style,
        durations_path=shoot / "durations.json",
        out_dir=tmp_path,
        live=True,
    )
    assert live.mode == "live"
    hosts = {s["url"] for s in seen}
    assert "https://api.anthropic.com/v1/messages" in hosts
    assert any("decisions" in url for url in hosts)
    assert "claude-opus-5-5" in {s["model"] for s in seen}
    by_lane = {d["lane"] for d in live.payload["decisions"]}
    assert by_lane == {"linear", "creative"}
    assert {d["source"] for d in live.payload["decisions"] if d["lane"] == "creative"} == {"opus:live"}
    assert {d["source"] for d in live.payload["decisions"] if d["lane"] == "linear"} == {"jev:live"}
    assert "live pick" in live.fcpxml.read_text()


def test_brief_without_length_uses_the_profile_target(shoot, small_style, tmp_path):
    out = assemble(
        media=shoot,
        brief="A test video",
        style=small_style,
        durations_path=shoot / "durations.json",
        out_dir=tmp_path,
    )
    assert out.payload["target_seconds"] == 480
    assert any("material ran out" in w for w in out.warnings)


def test_no_music_still_assembles(tmp_path, small_style):
    folder = tmp_path / "shoot"
    make_fixture(folder, use_ffmpeg=False)
    shutil.rmtree(folder / "music")
    out = assemble(media=folder, brief=BRIEF, style=small_style, durations_path=folder / "durations.json", out_dir=tmp_path / "o")
    assert out.payload["music"] == []
    assert any("no music" in w for w in out.warnings)
    if out.dtd_errors is not None:
        assert out.dtd_errors == []


def test_fcpxml_input_uses_its_assets(tmp_path, result, small_style):
    out = assemble(fcpxml=result.fcpxml, brief=BRIEF, style=small_style, out_dir=tmp_path)
    names = {f["name"] for f in out.payload["material"]["footage"]}
    assert "01_talk_hook.mp4" in names
    assert out.payload["material"]["songs"], "the music asset in the FCPXML is picked up"


def test_cli_assemble(shoot, tmp_path, capsys):
    code = main(
        [
            "assemble",
            "--media",
            str(shoot),
            "--brief",
            BRIEF,
            "--durations",
            str(shoot / "durations.json"),
            "--out-dir",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run assembly" in out and ".assembled.fcpxml" in out


def test_refuses_both_inputs(shoot, tmp_path):
    with pytest.raises(ConductorError, match="exactly one"):
        assemble(media=shoot, fcpxml=shoot / "x.fcpxml", brief=BRIEF, out_dir=tmp_path)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed")
@needs_lxml
def test_real_clips_through_ffprobe(tmp_path, small_style):
    folder = tmp_path / "real"
    info = make_fixture(folder, use_ffmpeg=True)
    assert info["real_clips"] is True
    out = assemble(media=folder, brief=BRIEF, style=small_style, out_dir=tmp_path / "o")
    footage = {f["name"]: f for f in out.payload["material"]["footage"]}
    assert footage["01_talk_hook.mp4"]["duration_source"] == "ffprobe"
    assert footage["03_broll_city.mp4"]["role"] == "b_roll"
    assert out.dtd_errors == []
    assert out.metrics["on_beat"]["ratio"] == 1.0


def test_reexport_records_style_parameters(result):
    from conductor.feedback import diff_fcpxml

    events, _warnings = diff_fcpxml(result.fcpxml, result.fcpxml)
    observed = [event for event in events if event.get("observer") == "style"]
    params = {event["param"]: event["value"] for event in observed}
    assert any(name.startswith("pacing.") and name.endswith(".asl_seconds") for name in params)
    assert params["music.fade_in.seconds"] > 0
    assert params["music.fade_out.seconds"] > 0
    shipped = load_style("byjustinwu")
    if shipped.music_section("talking")["duck"] and shipped.get("music.duck.enabled"):
        assert "music.duck.depth_db" in params
    assert all(event["event"] == "observe" for event in observed)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed")
@needs_lxml
def test_room_run_assembles_a_real_clip_folder_with_music(tmp_path):
    from conductor.room import find_assembler, room_run
    from conductor.validate import validate_fcpxml

    folder = tmp_path / "shoot"
    info = make_fixture(folder, use_ffmpeg=True)
    assert info["real_clips"] is True
    song = folder / "music" / "song_120bpm.wav"
    assert song.is_file()
    shutil.copy(song, folder / song.name)
    assert find_assembler().__name__ == "assemble"
    result = room_run(
        folder,
        out_root=tmp_path / "out",
        brief="A 40-second test edit about cutting on the beat.",
        style="byjustinwu",
    )
    assert result.payload["flow"] == "assemble+iterate"
    fcpxml = next((result.out_dir / "assemble").glob("*.assembled.fcpxml"))
    text = fcpxml.read_text(encoding="utf-8")
    assert validate_fcpxml(fcpxml) == []
    assert "SF Pro" in text and "adjust-volume" in text
    assert result.payload["decision_usage"]["engines"]["opus"]["items"] > 0
    assert result.payload["decision_usage"]["engines"]["jev"]["items"] > 0


def test_room_run_uses_the_installed_assembler(tmp_path):
    from conductor.room import find_assembler, room_run

    folder = tmp_path / "shoot"
    make_fixture(folder, use_ffmpeg=False)
    song = next((folder / "music").glob("*.wav"))
    shutil.copy(song, folder / song.name)
    assert find_assembler() is not None
    result = room_run(folder, out_root=tmp_path / "out", brief=BRIEF, style="byjustinwu")
    assert result.payload["flow"] == "assemble+iterate"
    assembled = result.out_dir / "assemble"
    fcpxml = next(assembled.glob("*.assembled.fcpxml"))
    text = fcpxml.read_text(encoding="utf-8")
    assert "SF Pro" in text and "adjust-volume" in text
    assert result.payload["decision_usage"]["engines"]["opus"]["items"] > 0

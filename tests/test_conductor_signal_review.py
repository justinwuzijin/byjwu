"""Media signals on real-export shapes: timecode, anchors, storylines, and word timings."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from conductor.apply import Deletion, apply_edits
from conductor.fcpxml import local, parse_fcpxml, parse_xml
from conductor.ingest import file_url
from conductor.iterate import iterate
from conductor.run import analyze
from conductor.signals import Loudness, ProbeResult, Word, gather
from conductor.timing import audible_spans
from conductor.words import PROTOCOL, timeline_words

BRIEF = "A tight interview. Keep the guest's story, lose dead air."

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    {assets}
  </resources>
  <library><event name="Room"><project name="Cut">
    <sequence format="r1" duration="{duration}s" tcStart="0s">
      <spine>
        {spine}
      </spine>
    </sequence>
  </project></event></library>
</fcpxml>"""


def _asset(asset_id: str, src: str, start: str = "0s", duration: str = "4000s") -> str:
    return (
        f'<asset id="{asset_id}" name="{asset_id}" start="{start}" duration="{duration}" '
        f'hasAudio="1" format="r1"><media-rep kind="original-media" src="{src}"/></asset>'
    )


def _doc(assets: str, spine: str, duration: int = 20):
    return parse_xml(HEAD.format(assets=assets, spine=spine, duration=duration))


def _by_name(doc):
    return {span.clip_name: span for span in audible_spans(doc, doc.sequences)}


def test_asset_timecode_is_subtracted_so_ffmpeg_seeks_the_file():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-cam.mov", start="3600s"),
        '<asset-clip ref="a1" offset="0s" name="Cam" start="3610s" duration="5s"/>',
    )
    span = _by_name(doc)["Cam"]
    assert span.media_bounds() == (Fraction(10), Fraction(15))
    assert span.timeline_bounds() == (Fraction(0), Fraction(5))


def test_connected_offset_is_on_the_parent_clock_including_on_a_gap():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-a.mov") + _asset("b1", "file:///tmp/no-such-b.mov"),
        """
        <asset-clip ref="a1" offset="0s" name="A" start="120s" duration="10s">
          <asset-clip ref="b1" lane="1" offset="125s" name="Broll" start="40s" duration="2s"/>
        </asset-clip>
        <gap offset="10s" name="Gap" start="3600s" duration="5s">
          <asset-clip ref="b1" lane="1" offset="3601s" name="VO" start="0s" duration="3s"/>
        </gap>
        """,
    )
    spans = _by_name(doc)
    assert spans["Broll"].timeline_bounds() == (Fraction(5), Fraction(7))
    assert spans["Broll"].media_bounds() == (Fraction(40), Fraction(42))
    assert spans["Broll"].connected is True
    assert spans["Broll"].clip_id == "s0c0k0"
    assert spans["VO"].timeline_bounds() == (Fraction(11), Fraction(14))
    assert spans["VO"].connected is True


def test_disabled_items_are_not_heard():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-a.mov"),
        """
        <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="10s">
          <asset-clip ref="a1" lane="-1" offset="1s" name="Muted" start="0s" duration="2s" enabled="0"/>
        </asset-clip>
        """,
    )
    assert "Muted" not in _by_name(doc)


def test_time_map_reads_from_the_clip_start_for_its_duration():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-a.mov"),
        """
        <asset-clip ref="a1" offset="0s" name="Fast" start="2s" duration="2s">
          <timeMap>
            <timept time="0s" value="0s" interp="linear"/>
            <timept time="10s" value="20s" interp="linear"/>
          </timeMap>
        </asset-clip>
        """,
    )
    span = _by_name(doc)["Fast"]
    assert span.timeline_bounds() == (Fraction(0), Fraction(2))
    assert span.media_bounds() == (Fraction(4), Fraction(8))


def test_secondary_storyline_is_connected_and_anchored():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-a.mov"),
        """
        <asset-clip ref="a1" offset="0s" name="A" start="100s" duration="10s">
          <spine lane="1" offset="103s">
            <asset-clip ref="a1" offset="0s" name="S1" start="50s" duration="2s"/>
            <asset-clip ref="a1" offset="2s" name="S2" start="60s" duration="1s"/>
          </spine>
        </asset-clip>
        """,
    )
    spans = _by_name(doc)
    assert spans["S1"].timeline_bounds() == (Fraction(3), Fraction(5))
    assert spans["S2"].timeline_bounds() == (Fraction(5), Fraction(6))
    assert spans["S2"].media_bounds() == (Fraction(60), Fraction(61))
    assert spans["S1"].connected and spans["S2"].connected


def test_split_keeps_connected_offsets_on_the_parent_clock():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-a.mov") + _asset("b1", "file:///tmp/no-such-b.mov"),
        """
        <asset-clip ref="a1" offset="0s" name="A" start="120s" duration="10s">
          <asset-clip ref="b1" lane="1" offset="126s" name="Broll" start="40s" duration="2s"/>
        </asset-clip>
        """,
        duration=10,
    )
    result = apply_edits(doc, [Deletion("c1", "Cut", Fraction(1), Fraction(3), "remove", "mechanical")])
    assert result.warnings == []
    pieces = _spine_elements(doc)
    assert [(p.get("offset"), p.get("start"), p.get("duration")) for p in pieces] == [
        ("0s", "120s", "1s"),
        ("1s", "123s", "7s"),
    ]
    assert [child.get("name") for child in pieces[0] if local(child.tag) == "asset-clip"] == []
    broll = [child for child in pieces[1] if local(child.tag) == "asset-clip"]
    assert [(child.get("name"), child.get("offset")) for child in broll] == [("Broll", "126s")]


def test_split_of_a_time_mapped_clip_moves_start_and_keeps_the_map():
    doc = _doc(
        _asset("a1", "file:///tmp/no-such-a.mov"),
        """
        <asset-clip ref="a1" offset="0s" name="Fast" start="2s" duration="4s">
          <timeMap>
            <timept time="0s" value="0s" interp="linear"/>
            <timept time="20s" value="40s" interp="linear"/>
          </timeMap>
        </asset-clip>
        """,
        duration=4,
    )
    apply_edits(doc, [Deletion("c1", "Cut", Fraction(0), Fraction(1), "remove", "mechanical")])
    piece = _spine_elements(doc)[0]
    assert (piece.get("offset"), piece.get("start"), piece.get("duration")) == ("0s", "3s", "3s")
    points = [(p.get("time"), p.get("value")) for p in piece.iter() if local(p.tag) == "timept"]
    assert points == [("0s", "0s"), ("20s", "40s")]
    reparsed = parse_xml(_xml(doc))
    assert _by_name(reparsed)["Fast"].media_bounds() == (Fraction(6), Fraction(12))


def test_connected_speech_blocks_a_spine_silence_and_music_does_not(tmp_path, monkeypatch):
    camera = _file(tmp_path, "camera.wav")
    lav = _file(tmp_path, "lav.wav")
    _pretend_ffmpeg(monkeypatch)

    def probe(path, start, end):
        if path.name == "camera.wav":
            return ProbeResult([(Fraction(1), Fraction(5))], Loudness(-30.0, -12.0, False))
        return ProbeResult([], Loudness(-20.0, -6.0, False))

    def spine(role: str) -> str:
        return f"""
        <asset-clip ref="cam" offset="0s" name="Cam" start="0s" duration="8s" audioRole="dialogue">
          <asset-clip ref="lav" lane="-1" offset="2s" name="Lav" start="0s" duration="4s" audioRole="{role}"/>
        </asset-clip>"""

    assets = _asset("cam", file_url(camera)) + _asset("lav", file_url(lav))
    blocked = gather(
        _doc(assets, spine("dialogue")), _doc(assets, spine("dialogue")).sequences,
        transcribe="off", cache_dir=tmp_path / "c1", probe=probe,
    )
    assert blocked.audio == "used"
    assert blocked.silences == []
    doc = _doc(assets, spine("music.music-1"))
    free = gather(doc, doc.sequences, transcribe="off", cache_dir=tmp_path / "c2", probe=probe)
    assert [(s.timeline_start, s.timeline_end) for s in free.silences] == [(Fraction(1), Fraction(5))]
    assert free.silences[0].integrated_lufs == -30.0


def test_sparse_use_of_a_long_file_decodes_only_the_used_ranges(tmp_path, monkeypatch):
    media = _file(tmp_path, "long.wav")
    _pretend_ffmpeg(monkeypatch)
    probes: list[tuple[Fraction, Fraction]] = []
    heard: list[tuple[Fraction, Fraction]] = []

    def probe(path, start, end):
        probes.append((start, end))
        return ProbeResult([], Loudness(-20.0, -6.0, False))

    def transcriber(path, start, end):
        heard.append((start, end))
        return [Word(start + 1, start + Fraction(3, 2), "hello")]

    doc = _doc(
        _asset("a1", file_url(media)) + _asset("m1", file_url(_file(tmp_path, "song.wav"))),
        """
        <asset-clip ref="a1" offset="0s" name="Open" start="10s" duration="5s">
          <asset-clip ref="m1" lane="-1" offset="10s" name="Song" start="0s" duration="5s" audioRole="music"/>
        </asset-clip>
        <asset-clip ref="a1" offset="5s" name="Close" start="3500s" duration="5s"/>
        """,
        duration=10,
    )
    report = gather(doc, doc.sequences, cache_dir=tmp_path / "cache", probe=probe, transcriber=transcriber, whisper_tool="test")
    silence_probes = [item for item in probes if item in {(Fraction(10), Fraction(15)), (Fraction(3500), Fraction(3505))}]
    assert len(silence_probes) >= 2
    assert all(end - start <= 5 for start, end in probes)
    assert heard == [(Fraction(9), Fraction(16)), (Fraction(3499), Fraction(3506))]
    assert [(w.text, w.start, w.clip_name) for w in report.words] == [
        ("hello", Fraction(0), "Open"),
        ("hello", Fraction(5), "Close"),
    ]
    probes.clear()
    heard.clear()
    again = gather(doc, doc.sequences, cache_dir=tmp_path / "cache", probe=probe, transcriber=transcriber, whisper_tool="test")
    assert probes == [] and heard == []
    assert again.cache_hits["silence"] == 2
    assert again.cache_hits["transcript"] == 1


def test_timeline_words_follow_the_clip_onto_another_fcpxml(tmp_path, monkeypatch):
    media = _file(tmp_path, "take.wav")
    _pretend_ffmpeg(monkeypatch)
    words = [
        Word(Fraction(12), Fraction("12.4"), "hello", 0.9),
        Word(Fraction("12.4"), Fraction(13), "world", 0.8),
    ]
    first = tmp_path / "v0.fcpxml"
    first.write_text(
        HEAD.format(
            assets=_asset("a1", file_url(media), start="3600s"),
            spine='<asset-clip ref="a1" offset="0s" name="Take" start="3610s" duration="5s"/>',
            duration=5,
        )
    )
    doc = parse_fcpxml(first)
    report = gather(
        doc, doc.sequences, signals="off", cache_dir=tmp_path / "cache",
        transcriber=lambda path, start, end: [w for w in words if start <= w.start < end], whisper_tool="test",
    )
    assert [(w.text, w.start, w.end) for w in report.words] == [
        ("hello", Fraction(2), Fraction("2.4")),
        ("world", Fraction("2.4"), Fraction(3)),
    ]
    moved = tmp_path / "v1.fcpxml"
    moved.write_text(
        HEAD.format(
            assets=_asset("a1", file_url(media), start="3600s"),
            spine=(
                '<gap offset="0s" name="Gap" start="0s" duration="4s"/>'
                '<asset-clip ref="a1" offset="4s" name="Take" start="3612s" duration="3s"/>'
            ),
            duration=7,
        )
    )
    found = timeline_words(moved, transcribe="cached", cache_dir=tmp_path / "cache")
    assert [(w.text, w.start, w.end, w.confidence) for w in found] == [
        ("hello", Fraction(4), Fraction("4.4"), 0.9),
        ("world", Fraction("4.4"), Fraction(5), 0.8),
    ]
    assert found[0].to_dict()["start"] == "4s"
    assert found[0].file_start == Fraction(12)
    assert timeline_words(moved, transcribe="cached", cache_dir=tmp_path / "empty") == []


def test_transcriber_and_probe_failures_are_skips(tmp_path, monkeypatch):
    media = _file(tmp_path, "take.wav")
    _pretend_ffmpeg(monkeypatch)
    doc = _doc(_asset("a1", file_url(media)), '<asset-clip ref="a1" offset="0s" name="Take" start="0s" duration="5s"/>', 5)

    def broken_probe(path, start, end):
        raise KeyError("decoder")

    def broken_model(path, start, end):
        raise KeyError("tokenizer")

    report = gather(doc, doc.sequences, cache_dir=tmp_path / "cache", probe=broken_probe, transcriber=broken_model, whisper_tool="test")
    assert report.audio == "skipped"
    assert report.transcript == "skipped"
    assert report.silences == [] and report.words == []
    assert any("transcription failed" in reason for reason in report.reasons)
    assert list(tmp_path.glob("cache/*.json")) == []


def test_no_ffmpeg_is_a_skip_and_the_run_still_writes(tmp_path, monkeypatch):
    media = _file(tmp_path, "take.wav")
    xml = tmp_path / "take.fcpxml"
    xml.write_text(HEAD.format(assets=_asset("a1", file_url(media)), spine='<asset-clip ref="a1" offset="0s" name="Take" start="0s" duration="5s"/>', duration=5))
    monkeypatch.setattr("conductor.signals.shutil.which", lambda name: None)
    report = analyze(xml, brief=BRIEF, out_dir=tmp_path / "out", signals="on", transcribe="on", signal_cache=tmp_path / "cache")
    signals = report.payload["signals"]
    assert signals["audio"] == "skipped" and signals["transcript"] == "skipped"
    assert "ffmpeg is not on PATH" in signals["summary"]
    assert report.warnings and "ffmpeg" in report.warnings[0]
    assert report.payload["files"]["words"] is None
    assert report.out_fcpxml is not None and report.out_fcpxml.is_file()


def test_iterate_writes_words_mapped_onto_the_applied_cut(tmp_path, monkeypatch):
    media = _file(tmp_path, "take.wav")
    xml = tmp_path / "take.fcpxml"
    xml.write_text(HEAD.format(assets=_asset("a1", file_url(media), duration="5s"), spine='<asset-clip ref="a1" offset="0s" name="Take" start="0s" duration="5s" audioRole="dialogue"/>', duration=5))
    _pretend_ffmpeg(monkeypatch)
    monkeypatch.setattr(
        "conductor.signals.probe_range",
        lambda path, start, end: ProbeResult(
            [(max(start, Fraction(1)), min(end, Fraction(4)))] if start < 4 and end > 1 else [],
            Loudness(-22.0, -9.0, False),
        ),
    )
    words = [Word(Fraction("0.2"), Fraction("0.6"), "hello"), Word(Fraction("4.2"), Fraction("4.6"), "world")]
    monkeypatch.setattr(
        "conductor.signals.resolve_whisper",
        lambda: ("test-whisper", lambda path, start, end: [w for w in words if w.end > start and w.start < end]),
    )
    result = iterate(fcpxml=xml, brief=BRIEF, out_dir=tmp_path / "out", max_rounds=2, signal_cache=tmp_path / "cache")
    first = result.rounds[0]
    assert first["cuts"] and all(cut["pass"] == "mechanical" for cut in first["cuts"])
    assert first["signals"]["transcript"] == "whisper"
    assert first["signals"]["word_count"] == 2
    payload = json.loads(Path(first["words"]).read_text())
    assert payload["protocol"] == PROTOCOL
    assert Path(first["words"]).name.endswith(".conductor.applied.words.json")
    assert [(w["text"], w["start"]) for w in payload["words"]] == [("hello", "1/5s"), ("world", "6/5s")]
    loop = json.loads(Path(result.out_json).read_text())
    assert loop["words"] == result.rounds[-1]["words"]


def _file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"stand-in media; the probe and transcriber are stubbed")
    return path


def _pretend_ffmpeg(monkeypatch) -> None:
    monkeypatch.setattr(
        "conductor.signals.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None
    )


def _spine_elements(doc):
    sequence = doc.sequences[0].element
    spine = next(child for child in sequence if local(child.tag) == "spine")
    return [child for child in spine if local(child.tag) == "asset-clip"]


def _xml(doc) -> str:
    import xml.etree.ElementTree as ET

    return ET.tostring(doc.tree.getroot(), encoding="unicode")

"""Media signals: time maps, cached probes, and a mechanical trim from silence."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from conductor.apply import Deletion, apply_edits
from conductor.candidates import AudioSilence
from conductor.fcpxml import local, parse_xml
from conductor.ingest import file_url
from conductor.iterate import iterate
from conductor.passes import collect
from conductor.run import analyze
from conductor.signals import (
    Loudness,
    ProbeResult,
    Word,
    _words_from_cpp,
    gather,
    probe_range,
    words_to_cues,
)
from conductor.timing import audible_spans
from conductor.transcript import Cue, parse_cues

BRIEF = "A tight interview. Keep the guest's story, lose dead air."
FFMPEG = shutil.which("ffmpeg")


def test_conform_maps_source_frames_onto_sequence_frames():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="a1" name="cam" start="0s" duration="30s" hasAudio="1" format="r1">
              <media-rep kind="original-media" src="file:///tmp/no-such-cam.mov"/>
            </asset>
          </resources>
          <project name="Cut">
            <sequence format="r1" duration="8s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="Wide" start="10s" duration="8s">
                  <conform-rate scaleEnabled="1" srcFrameRate="30"/>
                </asset-clip>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    span = audible_spans(doc, doc.sequences)[0]
    assert span.reachable is False
    assert span.connected is False
    bounds = span.media_bounds()
    assert bounds == (Fraction(10), Fraction(10) + Fraction(8) * Fraction(24, 30))
    timeline = span.timeline_bounds()
    assert timeline == (Fraction(0), Fraction(8))


def test_compound_window_maps_inner_media_onto_the_parent_timeline():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="a1" name="interview" start="0s" duration="200s" hasAudio="1" format="r1">
              <media-rep kind="original-media" src="file:///tmp/no-such-interview.mov"/>
            </asset>
            <media id="m1" name="Compound">
              <sequence format="r1" duration="10s" tcStart="0s">
                <spine>
                  <asset-clip ref="a1" offset="0s" name="Inner" start="100s" duration="10s"/>
                </spine>
              </sequence>
            </media>
          </resources>
          <project name="Cut">
            <sequence format="r1" duration="9s" tcStart="0s">
              <spine>
                <ref-clip ref="m1" offset="5s" name="Compound" start="2s" duration="4s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    spans = [span for span in audible_spans(doc, doc.sequences) if span.kind == "asset-clip"]
    assert len(spans) == 1
    span = spans[0]
    assert span.clip_name == "Inner"
    assert span.connected is False
    assert span.clip_id == doc.sequences[0].spine[0].id
    assert span.timeline_bounds() == (Fraction(5), Fraction(9))
    assert span.media_bounds() == (Fraction(102), Fraction(106))


def test_nested_compound_maps_through_both_windows():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="a1" name="interview" start="0s" duration="200s" hasAudio="1" format="r1">
              <media-rep kind="original-media" src="file:///tmp/no-such-interview.mov"/>
            </asset>
            <media id="m2" name="Inner">
              <sequence format="r1" duration="10s" tcStart="0s">
                <spine>
                  <asset-clip ref="a1" offset="0s" name="Deep" start="50s" duration="10s"/>
                </spine>
              </sequence>
            </media>
            <media id="m1" name="Mid">
              <sequence format="r1" duration="6s" tcStart="0s">
                <spine>
                  <ref-clip ref="m2" offset="0s" name="Mid" start="1s" duration="6s"/>
                </spine>
              </sequence>
            </media>
          </resources>
          <project name="Cut">
            <sequence format="r1" duration="3s" tcStart="0s">
              <spine>
                <ref-clip ref="m1" offset="0s" name="Outer" start="2s" duration="3s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    spans = [span for span in audible_spans(doc, doc.sequences) if span.clip_name == "Deep"]
    assert len(spans) == 1
    assert spans[0].timeline_bounds() == (Fraction(0), Fraction(3))
    assert spans[0].media_bounds() == (Fraction(53), Fraction(56))


def test_connected_audio_is_measured_and_is_not_a_spine_trim():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="a1" name="a" start="0s" duration="20s" hasAudio="1" format="r1">
              <media-rep kind="original-media" src="file:///tmp/no-such-a.mov"/>
            </asset>
          </resources>
          <project name="Cut">
            <sequence format="r1" duration="8s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="A" start="0s" duration="8s" audioRole="dialogue">
                  <asset-clip ref="a1" lane="1" offset="1s" name="Bed" start="3s" duration="4s"/>
                </asset-clip>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    spans = audible_spans(doc, doc.sequences)
    by_name = {span.clip_name: span for span in spans}
    assert by_name["A"].connected is False
    assert by_name["A"].timeline_bounds() == (Fraction(0), Fraction(8))
    assert by_name["Bed"].connected is True
    assert by_name["Bed"].timeline_bounds() == (Fraction(1), Fraction(5))
    assert by_name["Bed"].media_bounds() == (Fraction(3), Fraction(7))
    bed = by_name["Bed"]
    found = collect(
        doc.sequences,
        [],
        transcript_present=False,
        requested=["mechanical"],
        audio_silences=[
            AudioSilence(bed.clip_id, Fraction(1), Fraction(5), Fraction(3), Fraction(7))
        ],
    )
    assert found == []


def test_audio_slip_is_the_heard_range():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="a1" name="a" start="0s" duration="30s" hasAudio="1" format="r1">
              <media-rep kind="original-media" src="file:///tmp/no-such-a.mov"/>
            </asset>
          </resources>
          <project name="Cut">
            <sequence format="r1" duration="5s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="Slipped" start="10s" duration="5s"
                  audioStart="12s" audioDuration="5s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    span = audible_spans(doc, doc.sequences)[0]
    assert span.media_bounds() == (Fraction(12), Fraction(17))


def test_time_map_stretches_media_onto_the_timeline():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="a1" name="a" start="0s" duration="40s" hasAudio="1" format="r1">
              <media-rep kind="original-media" src="file:///tmp/no-such-a.mov"/>
            </asset>
          </resources>
          <project name="Cut">
            <sequence format="r1" duration="4s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="Retime" start="0s" duration="4s">
                  <timeMap>
                    <timept time="0s" value="10s" interp="linear"/>
                    <timept time="4s" value="18s" interp="linear"/>
                  </timeMap>
                </asset-clip>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    span = audible_spans(doc, doc.sequences)[0]
    assert span.media_bounds() == (Fraction(10), Fraction(18))
    from conductor.timing import timeline_intervals

    heard = timeline_intervals(span.pieces, Fraction(12), Fraction(14))
    assert heard == [(Fraction(1), Fraction(2))]


def test_conformed_split_keeps_the_source_frame():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.11">
      <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="a1" name="cam" start="0s" duration="30s" hasAudio="1" format="r1">
          <media-rep kind="original-media" src="file:///tmp/no-such-cam.mov"/>
        </asset>
      </resources>
      <project name="Cut">
        <sequence format="r1" duration="8s" tcStart="0s">
          <spine>
            <asset-clip ref="a1" offset="0s" name="Wide" start="0s" duration="8s">
              <conform-rate scaleEnabled="1" srcFrameRate="30"/>
            </asset-clip>
          </spine>
        </sequence>
      </project>
    </fcpxml>"""
    doc = parse_xml(xml)
    apply_edits(doc, [Deletion("c0001", "Cut", Fraction(0), Fraction(3), "remove", "mechanical")])
    sequence = doc.sequences[0].element
    assert sequence is not None
    spine = next(child for child in sequence if local(child.tag) == "spine")
    element = next(child for child in spine if local(child.tag) == "asset-clip")
    assert element.get("duration") == "5s"
    assert element.get("start") == "12/5s"
    assert element.get("offset") == "0s"


def test_restart_and_word_cues_feed_the_dialogue_pass():
    cues = words_to_cues(
        [
            Word(Fraction(0), Fraction("0.4"), "I"),
            Word(Fraction("0.4"), Fraction("0.8"), "think"),
            Word(Fraction("1.2"), Fraction("1.5"), "I"),
            Word(Fraction("1.5"), Fraction("1.8"), "think"),
            Word(Fraction("1.8"), Fraction("2.1"), "we"),
            Word(Fraction("2.1"), Fraction("2.5"), "should"),
            Word(Fraction(4), Fraction("4.4"), "um"),
        ]
    )
    assert [cue.text for cue in cues] == ["I think", "I think we should", "um"]
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Cut">
            <sequence format="r1" duration="8s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="Take" start="0s" duration="8s" audioRole="dialogue"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    found = collect(doc.sequences, cues, transcript_present=True, requested=["dialogue"])
    assert any(item.signals.get("restart") and item.transcript == "I think" for item in found)
    assert any(item.signals.get("pure_filler") and item.transcript == "um" for item in found)
    assert all(item.pass_name == "dialogue" for item in found)


def test_cpp_json_words_keep_their_offsets():
    words = _words_from_cpp(
        {
            "transcription": [
                {
                    "text": " um",
                    "offsets": {"from": 1000, "to": 1400},
                    "words": [
                        {"text": " um", "offsets": {"from": 1000, "to": 1400}},
                    ],
                }
            ]
        },
        Fraction(5),
    )
    assert words == [Word(Fraction(6), Fraction("6.4"), "um")]


def test_missing_media_is_a_skip_and_the_dry_run_still_finishes(tmp_path):
    report = analyze(
        "fixtures/sample_interview.fcpxml",
        transcript_path="fixtures/sample_interview.srt",
        brief=BRIEF,
        out_dir=tmp_path,
        signal_cache=tmp_path / "cache",
    )
    signals = report.payload["signals"]
    assert signals["audio"] == "skipped"
    assert signals["transcript"] == "file"
    assert signals["unreachable"]
    assert any(item["kind"] == "silence_gap" for item in report.candidates)
    assert "not on this machine" in signals["summary"] or any(
        "not on this machine" in reason or "no referenced media" in reason for reason in signals["reasons"]
    )


def test_signals_off_does_not_probe(tmp_path, monkeypatch):
    calls = {"n": 0}

    def _boom(path, start, end):
        calls["n"] += 1
        raise AssertionError("probe")

    monkeypatch.setattr("conductor.signals.probe_range", _boom)
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"not audio")
    xml = tmp_path / "cut.fcpxml"
    xml.write_text(_one_clip(wav, 5))
    doc = parse_xml(xml.read_text())
    report = gather(
        doc,
        doc.sequences,
        signals="off",
        transcribe="off",
        cache_dir=tmp_path / "cache",
        probe=_boom,
    )
    assert calls["n"] == 0
    assert report.audio == "skipped"
    assert "audio signals are off" in report.reasons


def test_supplied_transcript_skips_whisper(tmp_path):
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"not audio")
    xml = tmp_path / "cut.fcpxml"
    xml.write_text(_one_clip(wav, 5))
    doc = parse_xml(xml.read_text())

    def _transcribe(path, start, end):
        raise AssertionError("transcribe")

    report = gather(
        doc,
        doc.sequences,
        signals="off",
        transcribe="on",
        cache_dir=tmp_path / "cache",
        transcript_supplied=True,
        transcriber=_transcribe,
    )
    assert report.transcript == "file"
    assert report.cues == []


def test_cache_covers_a_later_narrower_range(tmp_path, monkeypatch):
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"not audio")
    xml = tmp_path / "cut.fcpxml"
    xml.write_text(_one_clip(wav, 5))
    doc = parse_xml(xml.read_text())
    calls = {"n": 0}

    def _probe(path, start, end):
        calls["n"] += 1
        return ProbeResult(
            [(Fraction(1), Fraction(4))],
            Loudness(-20.0, -6.0, False),
        )

    monkeypatch.setattr("conductor.signals.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    first = gather(
        doc,
        doc.sequences,
        signals="auto",
        transcribe="off",
        cache_dir=tmp_path / "cache",
        probe=_probe,
    )
    assert calls["n"] == 1
    assert first.audio == "used"
    assert len(first.silences) == 1
    assert first.silences[0].timeline_end - first.silences[0].timeline_start >= Fraction("1.25")
    second = gather(
        doc,
        doc.sequences,
        signals="auto",
        transcribe="off",
        cache_dir=tmp_path / "cache",
        probe=_probe,
    )
    assert calls["n"] == 1
    assert second.cache_hits["silence"] == 1
    assert second.silences == first.silences


def test_words_from_a_local_tool_become_dialogue_candidates(tmp_path, monkeypatch):
    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"not audio")
    xml = tmp_path / "cut.fcpxml"
    xml.write_text(_one_clip(wav, 8))
    monkeypatch.setattr("conductor.signals.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def _probe(path, start, end):
        return ProbeResult([], None)

    def _transcribe(path, start, end):
        return [
            Word(Fraction(0), Fraction("0.3"), "um"),
            Word(Fraction("0.35"), Fraction("0.7"), "the"),
            Word(Fraction("0.7"), Fraction(1), "story"),
            Word(Fraction(3), Fraction("3.4"), "you"),
            Word(Fraction("3.4"), Fraction("3.8"), "know"),
        ]

    doc = parse_xml(xml.read_text())
    report = gather(
        doc,
        doc.sequences,
        signals="auto",
        transcribe="auto",
        cache_dir=tmp_path / "cache",
        probe=_probe,
        transcriber=_transcribe,
        whisper_tool="test",
    )
    assert report.transcript == "whisper"
    found = collect(
        doc.sequences,
        report.cues,
        transcript_present=True,
        requested=["dialogue"],
    )
    texts = {item.transcript for item in found}
    assert "um" in texts
    assert any(item.label == "pause" for item in found)
    again = gather(
        doc,
        doc.sequences,
        signals="off",
        transcribe="auto",
        cache_dir=tmp_path / "cache",
        probe=_probe,
        transcriber=lambda *args: (_ for _ in ()).throw(AssertionError("retranscribe")),
        whisper_tool="test",
    )
    assert again.cache_hits["transcript"] == 1
    assert [cue.text for cue in again.cues] == [cue.text for cue in report.cues]


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH")
def test_long_silence_is_a_mechanical_trim_through_iterate(tmp_path):
    media = tmp_path / "take.wav"
    _synth_silence(media)
    xml = tmp_path / "take.fcpxml"
    xml.write_text(_one_clip(media, 5))
    before = media.read_bytes()
    result = iterate(
        fcpxml=xml,
        brief=BRIEF,
        out_dir=tmp_path / "out",
        max_rounds=3,
        signal_cache=tmp_path / "cache",
    )
    assert media.read_bytes() == before
    assert result.stop_reason == "no-progress"
    assert len(result.rounds) == 2
    cuts = result.rounds[0]["cuts"]
    assert cuts
    assert all(cut["pass"] == "mechanical" and cut["kind"] == "silence_gap" for cut in cuts)
    removed = sum(cut["end_seconds"] - cut["start_seconds"] for cut in cuts)
    assert removed >= 2.0
    assert result.rounds[0]["signals"]["audio"] == "used"
    assert "ffmpeg" in result.rounds[0]["signals"]["summary"]
    assert result.rounds[1]["applied_ids"] == []
    applied = parse_xml(Path(result.rounds[0]["applied_fcpxml"]).read_text())
    duration = applied.sequences[0].duration
    assert duration is not None and duration < Fraction(4)
    payload = json.loads(Path(result.rounds[0]["json"]).read_text())
    assert payload["signals"]["audio"] == "used"
    assert payload["signals"]["clips"]
    assert payload["signals"]["clips"][0]["integrated_lufs"] is not None
    assert any(item.get("signals", {}).get("audio") for item in payload["candidates"] if item["kind"] == "silence_gap")


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH")
def test_probe_range_on_a_synthetic_file(tmp_path):
    media = tmp_path / "take.wav"
    _synth_silence(media)
    result = probe_range(media, Fraction(0), Fraction(5))
    assert result.error is None
    assert result.silence
    quiet = max(end - start for start, end in result.silence)
    assert quiet >= Fraction("2.5")
    assert result.loudness is not None
    assert result.loudness.integrated_lufs is not None
    assert result.loudness.clipping is False


def test_srt_restart_is_review_not_mechanical():
    cues = parse_cues(
        "1\n00:00:00,000 --> 00:00:00,800\nI think\n\n"
        "2\n00:00:01,000 --> 00:00:03,000\nI think we should ship it\n"
    )
    assert isinstance(cues[0], Cue)
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Cut">
            <sequence format="r1" duration="8s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="Take" start="0s" duration="8s" audioRole="dialogue"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>"""
    )
    found = collect(doc.sequences, cues, transcript_present=True)
    restart = next(item for item in found if item.signals.get("restart"))
    assert restart.pass_name == "dialogue"
    assert restart.kind == "filler_pause"


def _one_clip(path: Path, seconds_long: int) -> str:
    src = file_url(path)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="a1" name="take" start="0s" duration="{seconds_long}s" hasVideo="0" hasAudio="1" format="r1" audioSources="1" audioChannels="1" audioRate="48000">
      <media-rep kind="original-media" src="{src}"/>
    </asset>
  </resources>
  <library>
    <event name="Room">
      <project name="Take">
        <sequence format="r1" duration="{seconds_long}s" tcStart="0s" tcFormat="NDF">
          <spine>
            <asset-clip ref="a1" offset="0s" name="Take" start="0s" duration="{seconds_long}s" audioRole="dialogue"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


def _synth_silence(path: Path) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("ffmpeg is not on PATH")
    completed = subprocess.run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=sample_rate=48000:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-filter_complex",
            "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not path.is_file():
        pytest.skip(f"ffmpeg could not synthesize audio: {completed.stderr[-200:]}")


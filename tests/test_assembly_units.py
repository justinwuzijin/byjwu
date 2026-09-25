"""Beats, PNG, subtitles, brief parsing, and music keyframes in isolation."""

from __future__ import annotations

import json
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from conductor.assembly.beats import detect_beats, synthetic_grid
from conductor.assembly.engine import parse_target
from conductor.assembly.png import read_image, write_png
from conductor.assembly.subtitles import apply_case, cards_from_cues, wrap
from conductor.assembly.synth import write_click_track
from conductor.transcript import Cue


def test_click_track_tempo_phase_and_downbeats(tmp_path):
    song = tmp_path / "click.wav"
    write_click_track(song, bpm=120.0, seconds=24.0)
    grid = detect_beats(song, duration=24.0)
    assert grid is not None and grid.source == "wav"
    assert grid.bpm == pytest.approx(120.0, abs=0.5)
    errors = [min(abs(b - k * 0.5) for k in range(60)) for b in grid.beats]
    assert max(errors) < 0.03
    assert grid.downbeats[0] == pytest.approx(0.0, abs=0.03)
    assert all(abs((d / 2.0) - round(d / 2.0)) < 0.02 for d in grid.downbeats[:5])


def test_off_beat_hats_do_not_steal_the_grid(tmp_path):
    song = tmp_path / "click.wav"
    write_click_track(song, bpm=96.0, seconds=20.0)
    grid = detect_beats(song, duration=20.0)
    period = 60 / 96
    phase = min(abs(b - round(b / period) * period) for b in grid.beats[:8])
    assert phase < 0.03


def test_sidecar_beats_win(tmp_path):
    song = tmp_path / "track.wav"
    write_click_track(song, bpm=120.0, seconds=4.0)
    (tmp_path / "track.beats.json").write_text(json.dumps({"bpm": 100, "beats": [0.1, 0.7, 1.3], "downbeats": [0.1]}))
    grid = detect_beats(song, duration=4.0)
    assert grid.source == "sidecar" and grid.bpm == 100 and grid.beats[0] == 0.1


def test_undecodable_music_falls_back_or_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr("conductor.assembly.beats.shutil.which", lambda _name: None)
    song = tmp_path / "song.mp3"
    song.write_bytes(b"not audio")
    assert detect_beats(song, duration=10.0) is None
    grid = detect_beats(song, duration=10.0, fallback_bpm=90.0)
    assert grid.source == "fallback" and grid.bpm == 90.0
    assert grid.beats[1] == pytest.approx(60 / 90)
    assert synthetic_grid(120, 2.0).downbeats == [0.0]


def test_png_round_trip(tmp_path):
    pixels = (np.arange(12 * 20 * 3) % 256).astype(np.uint8).reshape(12, 20, 3)
    path = tmp_path / "x.png"
    write_png(path, pixels)
    assert path.read_bytes().startswith(b"\x89PNG")
    assert np.array_equal(read_image(path), pixels)


def test_wav_reader_handles_stereo_24_bit(tmp_path):
    path = tmp_path / "s24.wav"
    rate = 22050
    tone = (np.sin(2 * np.pi * 440 * np.arange(rate) / rate) * 0.5 * 8388607).astype(np.int32)
    frames = bytearray()
    for sample in tone:
        b = int(sample).to_bytes(4, "little", signed=True)[:3]
        frames += b + b
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(3)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    from conductor.assembly.beats import decode

    samples = decode(path)
    assert samples is not None and abs(samples.max() - 0.5) < 0.02


def test_subtitle_cards_respect_line_rules():
    cues = [
        Cue(Fraction(0), Fraction(4), "This is a much longer sentence that has to be split into cards"),
        Cue(Fraction(4), Fraction(9, 2), "Um."),
        Cue(Fraction(5), Fraction(51, 10), "Ok"),
    ]
    cards = cards_from_cues(
        cues,
        max_chars=20,
        max_lines=1,
        min_seconds=Fraction(1, 2),
        max_seconds=Fraction(2),
        frame=Fraction(1, 24),
        case="lower",
    )
    assert all(len(line) <= 20 for card in cards for line in card.lines)
    assert all(card.text == card.text.lower() for card in cards)
    assert not any("um" == card.text.strip(".") for card in cards)
    assert cards[-1].duration >= Fraction(1, 2)
    for a, b in zip(cards, cards[1:]):
        assert a.end <= b.start
    assert all((card.start * 24).denominator == 1 for card in cards)


def test_wrap_and_case():
    assert wrap("one two three four", 9) == ["one two", "three", "four"]
    assert apply_case("hello WORLD", "sentence") == "Hello world"
    assert apply_case("hello world", "title") == "Hello World"
    assert apply_case("Hi", "upper") == "HI"


@pytest.mark.parametrize(
    ("brief", "seconds"),
    [
        ("an 8-minute video about cameras", 480.0),
        ("A 45s teaser", 45.0),
        ("a 90 second recap", 90.0),
        ("2.5 min explainer", 150.0),
        ("no length at all", None),
    ],
)
def test_parse_target(brief, seconds):
    assert parse_target(brief) == seconds


def test_fixture_writer_without_ffmpeg(tmp_path):
    from conductor.assembly.synth import SHOTS, make_fixture

    info = make_fixture(tmp_path / "shoot", use_ffmpeg=False)
    assert info["real_clips"] is False
    durations = json.loads(Path(info["durations"]).read_text())
    assert set(durations) == {shot.name for shot in SHOTS}
    assert (tmp_path / "shoot" / "01_talk_hook.srt").read_text().startswith("1\n00:00:00,300")

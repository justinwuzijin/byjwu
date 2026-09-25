"""Layer packing, silence trim, volume ramps, and opted-in cross dissolves.

The numbers follow @diffusionstudio/core (see conductor/assembly/layers.py).
Fixtures here are synthetic: generated samples and two short clips.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

import numpy as np
import pytest

from conductor.assembly.layers import (
    FFMPEG_MIN_SECONDS,
    SILENCE_MIN_SECONDS,
    SILENCE_THRESHOLD,
    SourceRange,
    audio_ramp_db,
    detect_silences,
    detector_comparison,
    layers_from_timeline,
    pack_sequential,
    remove_silences,
    transition_window,
)
from conductor.assembly.layers import LayerClip
from conductor.assembly.render import render
from conductor.assembly.timeline import Item, Media, Timeline
from conductor.style import load_style
from conductor.validate import validate_fcpxml
from conductor.fcpxml import write_document


def _clip(name: str, offset: Fraction, duration: Fraction, section: str = "montage") -> Item:
    media = Media(
        key=name,
        kind="video",
        name=name,
        src=f"file:///tmp/{name}.mp4",
        uid=name,
        duration=Fraction(30),
        width=1920,
        height=1080,
        frame_duration=Fraction(1, 24),
        has_audio=True,
    )
    return Item("clip", 0, offset, duration, section, name, media=media, start=Fraction(0))


def test_sequential_pack_closes_a_gap_and_parallel_keeps_offsets():
    frame = Fraction(1, 24)
    left = LayerClip(_clip("a", Fraction(0), Fraction(2)), SourceRange(Fraction(0), Fraction(2)))
    right = LayerClip(_clip("b", Fraction(5), Fraction(2)), SourceRange(Fraction(0), Fraction(2)))
    pack_sequential([left, right], frame)
    assert right.item.offset == Fraction(2)
    timeline = Timeline("t", frame, 1920, 1080, spine=[left.item, right.item])
    # A connected title sits beside the spine, not packed onto it.
    note = Item("gap", 2, Fraction(1), Fraction(1), "montage", "title")
    timeline.connected.append(note)
    layers = layers_from_timeline(timeline)
    assert layers[0].mode.value == "SEQUENTIAL"
    assert layers[1].mode.value == "PARALLEL"
    assert layers[1].clips[0].item.offset == Fraction(1)


def test_dissolve_window_is_centred_on_an_abutted_cut():
    start, mid, end = transition_window(Fraction(4), Fraction(4), Fraction(1))
    assert (start, mid, end) == (Fraction(7, 2), Fraction(4), Fraction(9, 2))


def test_silence_padding_matches_their_split_and_thresholds_differ():
    samples = np.ones(24000, dtype=np.float32)
    samples[4800:19200] = 0.0
    found = detect_silences(samples, 24000)
    assert len(found) == 1
    assert 4800 / 24000 <= found[0][0] < 19200 / 24000
    short = np.ones(24000, dtype=np.float32)
    short[4800:9600] = 0.0
    assert detect_silences(short, 24000) == []
    pieces = remove_silences(
        SourceRange(Fraction(0), Fraction(10)),
        [(Fraction(2), Fraction(4))],
        padding=Fraction(1, 2),
    )
    assert len(pieces) == 2
    assert pieces[0].end == Fraction(5, 2)
    assert pieces[1].start == Fraction(4) and pieces[1].end == Fraction(10)
    leading = remove_silences(SourceRange(Fraction(0), Fraction(5)), [(Fraction(0), Fraction(1))])
    assert leading[0].start == Fraction(1)
    trailing = remove_silences(SourceRange(Fraction(0), Fraction(5)), [(Fraction(4), Fraction(5))])
    assert trailing[0].end == Fraction(9, 2)
    compared = detector_comparison()
    assert compared["rms_threshold"] == SILENCE_THRESHOLD
    assert compared["ffmpeg_amplitude"] < SILENCE_THRESHOLD
    assert compared["rms_min_seconds"] == SILENCE_MIN_SECONDS
    assert compared["ffmpeg_min_seconds"] == FFMPEG_MIN_SECONDS
    assert SILENCE_MIN_SECONDS > FFMPEG_MIN_SECONDS


def test_audio_ramp_is_floor_to_bed_in_decibels():
    keys = audio_ramp_db(Fraction(8), Fraction(1), Fraction(2), bed_db=-10, floor_db=-96)
    assert [key.db for key in keys] == [-96, -10, -10, -96]
    assert keys[0].interp == "easeIn" and keys[2].interp == "easeOut"
    assert audio_ramp_db(Fraction(4), Fraction(0), Fraction(0), bed_db=-10, floor_db=-96) == []


def test_profile_dissolve_becomes_a_dtd_valid_cross_dissolve(tmp_path):
    profile = load_style("base")
    assert profile.get("cuts.dissolve.sections") == []
    frame = Fraction(1, 24)
    timeline = Timeline(
        "dissolve",
        frame,
        1920,
        1080,
        spine=[
            _clip("a", Fraction(0), Fraction(4), "montage"),
            _clip("b", Fraction(4), Fraction(4), "montage"),
        ],
    )
    plain = render(timeline)
    assert list(plain.getroot().iter("transition")) == []
    tree = render(timeline, dissolve_sections={"montage"}, dissolve_seconds=1.0)
    root = tree.getroot()
    transition = next(root.iter("transition"))
    assert transition.get("name") == "Cross Dissolve"
    assert transition.get("duration") == "1s"
    effect = next(el for el in root.iter("effect") if el.get("name") == "Cross Dissolve")
    video = next(transition.iter("filter-video"))
    assert video.get("ref") == effect.get("id")
    sequence = next(root.iter("sequence"))
    assert sequence.get("duration") == "7s"
    path = tmp_path / "dissolve.fcpxml"
    write_document(tree, path)
    errors = validate_fcpxml(path)
    if errors is None:
        pytest.importorskip("lxml")
    assert errors == []
    text = ET.tostring(root, encoding="unicode")
    assert "Cross Dissolve" in text

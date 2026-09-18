"""Tier 1: exactness, caching, and keeping regex-obvious filler out of tier 2."""

from __future__ import annotations

import json

import pytest
from conftest import synth_transcript

from cutmcp import extract


def test_ingest_is_idempotent_and_second_call_hits_cache(media, media_path, monkeypatch):
    """Gate 1. Same media_id, and no transcript is read the second time."""

    def explode(_p):
        raise AssertionError("second ingest re-extracted instead of hitting cache")

    monkeypatch.setattr(extract, "_load_sidecar", explode)
    monkeypatch.setattr(extract, "_transcribe", explode)

    again = extract.ingest(media_path)
    assert again.media_id == media.media_id
    assert again.to_dict() == media.to_dict()


def test_force_bypasses_cache(media, media_path, monkeypatch):
    seen = []
    real = extract._load_sidecar
    monkeypatch.setattr(extract, "_load_sidecar", lambda p: (seen.append(p), real(p))[1])

    again = extract.ingest(media_path, force=True)
    assert seen, "force=True should have re-read the transcript"
    assert again.media_id == media.media_id


def test_load_by_media_id_round_trips(media):
    loaded = extract.load(media.media_id)
    assert loaded.to_dict() == media.to_dict()
    assert extract.Media.from_dict(media.to_dict()).to_dict() == media.to_dict()


def test_trivial_filler_is_flagged_only_when_the_whole_line_is_filler():
    assert extract.is_trivial_filler("Um.")
    assert extract.is_trivial_filler("uh, um")
    assert extract.is_trivial_filler("You know.")
    assert extract.is_trivial_filler("Uh, you know, um.")
    assert not extract.is_trivial_filler("Um, the migration failed because of the replica lag.")
    assert not extract.is_trivial_filler("You know what broke it?")
    # deliberately not treated as trivial: often load-bearing
    assert not extract.is_trivial_filler("Yeah.")
    assert not extract.is_trivial_filler("Right.")


def test_fixture_contains_both_flagged_and_unflagged_segments(media):
    flagged = [s for s in media.segments if s.is_trivial_filler]
    assert 10 < len(flagged) < len(media.segments) / 2
    assert all(extract.is_trivial_filler(s.text) for s in flagged)


def test_segments_are_ordered_indexed_and_measured(media):
    assert len(media.segments) == 600
    assert [s.idx for s in media.segments] == list(range(600))
    for prev, seg in zip(media.segments, media.segments[1:]):
        assert seg.start >= prev.start
        assert seg.duration >= 0
        assert seg.gap_before == pytest.approx(max(0.0, seg.start - prev.end))
        assert prev.gap_after == pytest.approx(seg.gap_before)
    assert media.segments[0].gap_before == 0.0, "nothing precedes the first segment"
    assert media.speakers == ["GUEST", "HOST"]
    assert all(s.words > 0 for s in media.segments)


def test_duration_falls_back_to_last_segment_end_without_ffprobe(media):
    """The stub mp4 is undecodable, so ffprobe cannot answer."""
    assert media.duration == pytest.approx(media.segments[-1].end)
    assert media.segments[-1].gap_after == 0.0


def test_media_id_changes_when_the_file_changes(tmp_path):
    p = tmp_path / "a.mp4"
    p.write_bytes(b"\x00" * 16)
    first = extract.media_id_for(p)
    assert first == extract.media_id_for(p)
    p.write_bytes(b"\x00" * 32)
    assert extract.media_id_for(p) != first


def test_missing_transcript_names_both_options(tmp_path, monkeypatch):
    monkeypatch.setattr(extract.shutil, "which", lambda _: None)
    p = tmp_path / "orphan.mp4"
    p.write_bytes(b"\x00" * 16)
    with pytest.raises(RuntimeError, match=r"whisperx.*whisper"):
        extract.ingest(p)


def test_out_of_order_transcript_is_sorted(tmp_path):
    doc = synth_transcript(n=12, seed=3)
    doc["segments"].reverse()
    p = tmp_path / "rev.mp4"
    p.write_bytes(b"\x00" * 16)
    (tmp_path / "rev.json").write_text(json.dumps(doc))
    m = extract.ingest(p)
    assert [s.start for s in m.segments] == sorted(s.start for s in m.segments)

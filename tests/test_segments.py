"""Segment index, one-call Jev picking, hook, and chapter markers."""

from __future__ import annotations

import json
from fractions import Fraction

import httpx

from conductor.assembly.render import render
from conductor.assembly.timeline import Item, Media, Section, Timeline
from conductor.router import Router, classify
from conductor.segments import (
    find_bit,
    index_words,
    keyword_match_text,
    limit_candidates,
    pick,
    place_hook,
    segment_words,
    settings_from_profile,
)
from conductor.segments import SegmentSettings
from conductor.style import load_style

FRAME = Fraction(1, 30)


class Word:
    def __init__(self, text, start, end):
        self.text = text
        self.start = Fraction(start)
        self.end = Fraction(end)


def _words(lines):
    """``lines`` is (text, start, end) for each word."""
    return [Word(text, start, end) for text, start, end in lines]


def _clock(step, tokens, origin=0):
    rows = []
    cursor = Fraction(origin)
    for token in tokens:
        rows.append((token, cursor, cursor + Fraction(2, 5)))
        cursor += Fraction(step)
    return _words(rows)


def test_topic_boundary_and_max_length():
    topic = []
    topic.extend(_clock(1, ["alpha", "alpha", "alpha."], 0))
    topic.extend(_clock(1, ["alpha", "alpha", "alpha."], 4))
    topic.extend(_clock(1, ["bravo", "bravo", "bravo."], 12))
    topic.extend(_clock(1, ["bravo", "bravo", "bravo."], 16))
    settings = SegmentSettings(max_seconds=45, pause_seconds=0.8, chapter_threshold=0.05)
    segments = segment_words(topic, source="talk", settings=settings)
    assert any(segment.topic_depth >= 0.05 and "bravo" in segment.text for segment in segments)
    long = _clock(1, ["alpha."] * 60, 0)
    capped = segment_words(long, source="talk", settings=SegmentSettings(max_seconds=45, pause_seconds=5))
    assert len(capped) >= 2
    for segment in capped:
        assert float(segment.duration) <= 45
        assert any(word.start == segment.start for word in long)
        assert any(word.end == segment.end for word in long)


def test_cache_hit_reuses_the_index(tmp_path):
    words = _clock(1, ["alpha", "alpha", "ships."], 0)
    first, hit = index_words(words, source="talk", media_hash="abc123", cache_dir=tmp_path)
    assert hit is False
    second, hit = index_words([], source="talk", media_hash="abc123", cache_dir=tmp_path)
    assert hit is True
    assert [segment.text for segment in second] == [segment.text for segment in first]
    stored = json.loads((tmp_path / "segments" / "abc123.json").read_text(encoding="utf-8"))
    assert stored["media_hash"] == "abc123"
    assert "Users" not in json.dumps(stored)


def test_mocked_jev_picks_in_one_call(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("CONDUCTOR_DRY_RUN", raising=False)
    segments = []
    for index in range(260):
        text = "kyoto train ride" if index == 7 else f"office desk note {index}"
        segments.append(
            type("S", (), {
                "id": f"talk-s{index:04d}",
                "text": text,
                "keywords": tuple(text.split()[:3]),
                "source": "talk",
            })()
        )
    seen = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen["calls"] = seen.get("calls", 0) + 1
        seen["count"] = len(payload["state"]["segments"])
        seen["ids"] = [row["id"] for row in payload["state"]["segments"]]
        answers = {
            key: {"type": "noul", "noul": 0.91 if key == "talk-s0007" else 0.05}
            for key in payload["questions"]
        }
        return httpx.Response(200, json={"model": "typesafe/jev-1.13", "answers": answers})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    with Router(live=True, jev_client=client) as router:
        result = pick("kyoto train", segments, router, brief="kyoto train")
    assert seen["calls"] == 1
    assert seen["count"] == 250
    assert "talk-s0007" in seen["ids"]
    assert result["ids"] == ["talk-s0007"]
    assert result["scores"]["talk-s0007"] >= 0.5
    assert result["reason"]
    assert result["prefiltered"] is True
    assert result["engine"] == "jev"
    assert classify("segment_pick").engine == "jev"


def test_prefilter_keeps_the_lexical_neighbour():
    rows = [{"id": f"s{index:04d}", "text": f"note {index}", "keywords": []} for index in range(300)]
    rows[12] = {"id": "s0012", "text": "kyoto train", "keywords": ["kyoto", "train"]}
    kept, trimmed = limit_candidates("kyoto train", rows, cap=250)
    assert trimmed is True
    assert len(kept) == 250
    assert any(row["id"] == "s0012" for row in kept)


def test_dry_run_lexical_pick_is_stable():
    from conductor.segments import Segment

    segments = [
        Segment("talk-s0001", "talk", Fraction(0), Fraction(4), "kyoto train ride", ("kyoto", "train")),
        Segment("talk-s0002", "talk", Fraction(5), Fraction(9), "office desk lamp", ("office", "desk")),
    ]
    with Router(live=False) as router:
        first = pick("kyoto train", segments, router)
        second = pick("kyoto train", segments, router)
    assert first["ids"] == second["ids"] == ["talk-s0001"]
    assert first["source"] == "mock"
    assert first["reason"] == "dry-run lexical match"


def test_hook_and_chapters_land_in_fcpxml():
    words = []
    words.extend(_clock(1, ["alpha", "alpha", "alpha."], 0))
    words.extend(_clock(1, ["alpha", "alpha", "alpha."], 4))
    words.extend(_clock(Fraction(9, 10), ["kyoto", "train", "ride", "through", "the", "city."], 20))
    words.extend(_clock(1, ["bravo", "bravo", "bravo."], 30))
    words.extend(_clock(1, ["bravo", "bravo", "bravo."], 34))
    settings = SegmentSettings(
        max_seconds=45,
        pause_seconds=0.8,
        chapter_threshold=0.05,
        hook_enabled=True,
        chapters_enabled=True,
        hook_query="kyoto train",
    )
    segments = segment_words(words, source="talk", settings=settings)
    assert segments
    media = Media(
        key="talk",
        kind="video",
        name="talk",
        src="talk.mov",
        uid="talk-uid",
        duration=Fraction(60),
        has_audio=True,
    )
    spine = Item(
        kind="clip",
        lane=0,
        offset=Fraction(0),
        duration=Fraction(20),
        section="intro",
        name="talk",
        media=media,
        start=Fraction(30),
        role="dialogue",
    )
    timeline = Timeline(
        name="cut",
        frame=FRAME,
        width=1920,
        height=1080,
        spine=[spine],
        sections=[Section("intro", 0, "Intro", Fraction(0), Fraction(40), Fraction(40))],
    )
    with Router(live=False) as router:
        from conductor.segments import choose_hook, mark_chapters

        hook = choose_hook(segments, {"talk": words}, router, settings, brief="kyoto train")
        assert hook is not None
        assert 3 <= float(hook.duration) <= 8
        place_hook(timeline, hook, media)
        chapters = mark_chapters(timeline, segments, settings)
        markers = find_bit("kyoto train", segments, router)
    assert chapters >= 1
    assert markers
    assert timeline.spine[0].tags.get("hook") is True
    assert timeline.spine[0].offset == 0
    assert timeline.spine[1].offset > 0
    tree = render(timeline)
    xml = _xml(tree)
    assert "<chapter-marker" in xml
    assert "kyoto" in xml
    markers_on = [note for item in timeline.spine for note in item.notes if not note.chapter]
    assert markers_on == []
    from conductor.segments import mark_findings

    assert mark_findings(timeline, markers) >= 1
    xml = _xml(render(timeline))
    assert "<marker " in xml


def test_segment_keywords_can_extend_broll_match_text():
    from conductor.assembly.broll import BrollClip, Density, Slot, rank_clips
    from conductor.segments import Segment

    segments = [Segment("talk-s0001", "talk", Fraction(0), Fraction(4), "kyoto train", ("kyoto", "train"))]
    extra = keyword_match_text(segments)
    assert "kyoto" in extra
    slot = Slot(
        id="s0001",
        keyword="city",
        start=Fraction(0),
        score=1,
        window="a city",
        host_start=Fraction(0),
        host_end=Fraction(10),
        line="a city",
    )
    clip = BrollClip(
        id="b1",
        name="train",
        media_key="train",
        source_start=Fraction(0),
        source_end=Fraction(4),
        description="kyoto train interior",
    )
    plain = rank_clips(slot, [clip], density=Density(), used=[])
    boosted = rank_clips(slot, [clip], density=Density(), used=[], extra_match=extra)
    assert boosted
    assert boosted[0].score > (plain[0].score if plain else 0)


def test_style_defaults_enable_hook_and_chapters():
    assert settings_from_profile(None).hook_enabled is True
    assert settings_from_profile(None).chapters_enabled is True
    found = settings_from_profile(load_style("styles/base/profile.json"))
    assert found.hook_enabled is True
    assert found.chapters_enabled is True
    assert found.max_seconds == 45


def _xml(tree) -> str:
    import xml.etree.ElementTree as ET

    return ET.tostring(tree.getroot(), encoding="unicode")

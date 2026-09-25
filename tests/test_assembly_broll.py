"""Keyword B-roll slots: match, spacing, source reuse, veto, FCPXML offset."""

from __future__ import annotations

from fractions import Fraction

from conductor.assembly.broll import (
    HARD_MAX,
    HARD_MIN,
    BrollClip,
    Density,
    SpokenWord,
    plan_cutaways,
)
from conductor.assembly.render import render
from conductor.assembly.timeline import Item, Media, Timeline
from conductor.router import Router
from conductor.timeutil import parse_time

FRAME = Fraction(1, 24)


def _words(text: str, start: float, step: float, *, host_end: float, line: str | None = None) -> list[SpokenWord]:
    tokens = text.split()
    host_start = Fraction(0)
    end = Fraction(str(host_end))
    spoken = []
    cursor = Fraction(str(start))
    for token in tokens:
        spoken.append(
            SpokenWord(
                token,
                cursor,
                cursor + Fraction(str(step)),
                host_start,
                end,
                line if line is not None else text,
            )
        )
        cursor += Fraction(str(step))
    return spoken


def _clip(ident: str, description: str, *, media: str | None = None, start: float = 0, end: float = 8) -> BrollClip:
    return BrollClip(
        id=ident,
        name=ident,
        media_key=media or ident,
        source_start=Fraction(str(start)),
        source_end=Fraction(str(end)),
        description=description,
        has_audio=True,
    )


def _kyoto():
    words = _words("we took the train to Kyoto", 2.0, 0.5, host_end=12)
    clips = [
        _clip("train", "train interior"),
        _clip("ramen", "ramen bowl"),
        _clip("temple", "temple gate"),
    ]
    return words, clips


def test_train_slot_matches_the_train_interior():
    words, clips = _kyoto()
    placements, _decisions, _receipts, applied = plan_cutaways(words, clips, frame=FRAME)
    assert applied
    kept = [row for row in placements if not row.vetoed]
    assert kept
    assert kept[0].slot.keyword == "train"
    assert kept[0].clip.id == "train"
    assert "train" in kept[0].reason


def test_spacing_and_duration_stay_inside_the_density_rules():
    names = (
        "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
    ).split()
    words = _words(" ".join(names), 2.0, 1.0, host_end=30)
    clips = [_clip(name, f"{name} exterior", end=10) for name in names]
    density = Density(spacing_seconds=9, max_hold_seconds=20, target_seconds=3)
    placements, _decisions, _receipts, applied = plan_cutaways(words, clips, density=density, frame=FRAME)
    assert applied
    kept = [row for row in placements if not row.vetoed]
    starts = [row.timeline_start for row in kept]
    assert len(starts) >= 2
    for left, right in zip(starts, starts[1:]):
        assert right - left >= Fraction(9)
    for row in kept:
        assert HARD_MIN - 1e-9 <= float(row.duration) <= HARD_MAX + 1e-9


def test_slots_do_not_reuse_a_source_range():
    words = _words("we took the train to Kyoto", 2.0, 0.5, host_end=12)
    later = _words("another train crosses the bridge", 14.0, 0.5, host_end=24)
    clips = [
        _clip("long", "train interior carriage", media="reel", start=0, end=8),
        _clip("short", "train interior seats", media="reel", start=1, end=5),
        _clip("ramen", "ramen bowl", media="food", start=0, end=6),
    ]
    density = Density(spacing_seconds=9, max_hold_seconds=30, target_seconds=3)
    placements, _decisions, _receipts, applied = plan_cutaways(
        words + later, clips, density=density, frame=FRAME
    )
    assert applied
    kept = [row for row in placements if not row.vetoed]
    ranges = [(row.clip.media_key, row.source_start, row.source_end) for row in kept]
    for index, (key, start, end) in enumerate(ranges):
        for other_key, other_start, other_end in ranges[index + 1 :]:
            if key == other_key:
                assert not (start < other_end and other_start < end)


def test_a_vetoed_slot_places_no_connected_clip():
    words, clips = _kyoto()
    placements, decisions, _receipts, applied = plan_cutaways(
        words,
        clips,
        frame=FRAME,
        router=Router(live=False),
        mock_value="veto",
        brief="synthetic",
    )
    assert applied
    assert placements and all(row.vetoed for row in placements)
    assert any(row.value == "veto" for row in decisions.values())
    timeline = _timeline(words, placements)
    assert timeline.connected == []


def test_connected_offset_is_the_frame_snapped_start_word():
    words, clips = _kyoto()
    placements, _decisions, _receipts, _applied = plan_cutaways(words, clips, frame=FRAME)
    kept = next(row for row in placements if not row.vetoed)
    train = next(word for word in words if word.text == "train")
    snapped = Fraction(round(train.start / FRAME)) * FRAME
    assert kept.timeline_start == snapped
    timeline = _timeline(words, [kept])
    root = render(timeline).getroot()
    spine = root.find(".//spine")
    host = next(child for child in spine if child.get("name") == "host")
    cutaway = next(child for child in host if child.get("lane") == "1")
    assert parse_time(cutaway.get("offset")) == snapped
    assert parse_time(host.get("offset")) == Fraction(0)


def _timeline(words: list[SpokenWord], placements) -> Timeline:
    host_end = max(word.host_end for word in words)
    media = Media(
        key="host",
        kind="video",
        name="host",
        src="file:///tmp/synthetic-host.mp4",
        uid="host",
        duration=host_end,
        has_video=True,
        has_audio=True,
    )
    spine = Item(
        kind="clip",
        lane=0,
        offset=Fraction(0),
        duration=host_end,
        section="talking",
        name="host",
        media=media,
        start=Fraction(0),
        role="dialogue",
        tags={"speech": True},
    )
    timeline = Timeline("synthetic", FRAME, 1920, 1080, spine=[spine])
    for row in placements:
        if row.vetoed or row.duration <= 0:
            continue
        broll = Media(
            key=row.clip.media_key,
            kind="video",
            name=row.clip.name,
            src=f"file:///tmp/synthetic-{row.clip.id}.mp4",
            uid=row.clip.id,
            duration=row.clip.source_end,
            has_video=True,
            has_audio=True,
        )
        timeline.connected.append(
            Item(
                kind="clip",
                lane=1,
                offset=row.timeline_start,
                duration=row.duration,
                section="talking",
                name=row.clip.name,
                media=broll,
                start=row.source_start,
                role="effects",
                volume_db=-20,
                tags={"cutaway": True},
            )
        )
    return timeline


def test_duration_clamp_helper_bounds():
    density = Density()
    assert density.clamped_duration(0.1) == HARD_MIN
    assert density.clamped_duration(20) == HARD_MAX
    assert HARD_MIN <= density.clamped_duration(3) <= HARD_MAX

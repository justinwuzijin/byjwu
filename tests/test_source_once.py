"""A take's source range is reserved once, and a short gap does not split a hold."""

from __future__ import annotations

from fractions import Fraction

from conductor.assembly.layout import Layout
from conductor.assembly.select import _holds
from conductor.style import load_style
from conductor.transcript import Cue


def _layout() -> Layout:
    layout = object.__new__(Layout)
    layout.frame = Fraction(1, 24)
    layout.source_used = {}
    return layout


def test_the_head_of_a_take_is_not_claimed_twice():
    layout = _layout()
    first = layout._claim_source(4, Fraction(0), Fraction(12))
    second = layout._claim_source(4, Fraction(0), Fraction(24))
    third = layout._claim_source(4, Fraction(0), Fraction(12))
    assert first == (Fraction(0), Fraction(12))
    assert second == (Fraction(12), Fraction(24))
    assert third is None
    assert first[1] <= second[0]


def test_a_misheard_topic_does_not_break_a_short_hold(monkeypatch):
    def topic(_segments, cue):
        return "alpha" if cue.start < 2 else "beta"

    monkeypatch.setattr("conductor.assembly.select._topic_segments", lambda footage, cues: [object()])
    monkeypatch.setattr("conductor.assembly.select._topic_at", topic)
    profile = load_style("byjustinwu")
    close = [
        Cue(Fraction(0), Fraction(1), "The station picture holds the first thought."),
        Cue(Fraction("1.2"), Fraction("3.2"), "A split word should not open a new shot."),
    ]
    holds = _holds(object(), close, Fraction(45), profile, Fraction("0.7"))
    assert len(holds) == 1

    paused = [
        Cue(Fraction(0), Fraction(1), "The station picture holds the first thought."),
        Cue(Fraction("2.0"), Fraction(4), "A real pause is a new hold."),
    ]
    split = _holds(object(), paused, Fraction(45), profile, Fraction("0.7"))
    assert len(split) == 2


def test_word_timings_keep_a_breath_inside_the_take(monkeypatch):
    monkeypatch.setattr("conductor.assembly.select._topic_segments", lambda footage, cues: [object()])
    monkeypatch.setattr(
        "conductor.assembly.select._topic_at",
        lambda segments, cue: "one" if float(cue.start) < 5 else "two",
    )
    profile = load_style("byjustinwu")
    footage = type("F", (), {"signals": type("S", (), {"words": [object()]})()})()
    breath = [
        Cue(Fraction(0), Fraction(3), "I leave a short breath and I am into the next idea."),
        Cue(Fraction("4.2"), Fraction("6.4"), "The breath is part of the line not a cut."),
    ]
    assert len(_holds(footage, breath, Fraction(45), profile, Fraction("0.7"))) == 1
    pause = [
        Cue(Fraction(0), Fraction(3), "I leave a short breath and I am into the next idea."),
        Cue(Fraction("6.2"), Fraction(9), "A new subject starts after the long pause."),
    ]
    assert len(_holds(footage, pause, Fraction(45), profile, Fraction("0.7"))) == 2

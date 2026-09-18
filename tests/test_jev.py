"""The wire contract, pinned against TypeSafe 0.2.0's published schema.

Fixtures here are copied from `https://api.typesafe.ai/openapi.json`. If the
API moves, this file is where it should break first — nothing outside
`jev.py` is supposed to know the shape.
"""

from __future__ import annotations

import pytest

from cutmcp import jev

LEVELS = ("flat", "low", "steady", "engaged", "electric")


def test_questions_put_options_under_criteria():
    assert jev.noul("Is this spam?") == {"type": "noul", "instructions": "Is this spam?"}
    assert jev.noul("Spam?", true="unsolicited", false="legitimate")["criteria"] == {
        "true": "unsolicited",
        "false": "legitimate",
    }
    s = jev.score("How energetic?", LEVELS)
    assert s["criteria"] == list(LEVELS) and "levels" not in s
    c = jev.choice("What role?", {"hook": "opens attention"})
    assert "options" not in c
    assert c["criteria"] == {"hook": "opens attention", "none": "none of these apply"}
    assert "none" not in jev.choice("What role?", ["hook"], add_none=False)["criteria"]


def test_option_cardinality_is_capped_after_none_is_added():
    assert jev.MAX_OPTIONS == 250
    ok = [f"o{i}" for i in range(jev.MAX_OPTIONS - 1)]
    assert len(jev.choice("pick", ok)["criteria"]) == jev.MAX_OPTIONS
    with pytest.raises(ValueError, match="chunk"):
        jev.choice("pick", ok + ["one-too-many"])
    with pytest.raises(ValueError):
        jev.score("rate", ["only-one-level"])


def test_parses_a_documented_response():
    questions = {
        "keep": jev.noul("does it belong?"),
        "energy": jev.score("how energetic?", LEVELS),
        "role": jev.choice("what role?", {"hook": "opens", "claim": "asserts"}, add_none=False),
    }
    payload = {
        "model": "jev-latest",
        "answers": {
            "keep": {"type": "noul", "noul": 0.98},
            "energy": {
                "type": "score",
                "score": 1.7,
                "confidence": 0.9,
                "legend": {"0": "flat", "1": "low", "2": "steady"},
                "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
            },
            "role": {
                "type": "choice",
                "choice": "hook",
                "confidence": 0.9,
                "probabilities": {"hook": 0.8, "claim": 0.2},
            },
        },
        "usage": {"input_tokens": 120, "output_tokens": 12},
    }
    a = jev._parse(payload, questions)

    assert a["keep"].value == 0.98
    assert a["keep"].confidence == 0.98, "a noul states no confidence; imply it from p"
    assert a["energy"].value == 1.7, "a score is an expected value, not an index"
    assert a["energy"].confidence == 0.9
    assert a["energy"].probabilities == {"flat": 0.1, "low": 0.1, "steady": 0.8}
    assert a["role"].value == "hook"
    assert a["role"].probabilities == {"hook": 0.8, "claim": 0.2}


def test_parsing_stays_forgiving():
    questions = {"keep": jev.noul("?"), "energy": jev.score("?", LEVELS)}
    # older/bare shapes, and `results` instead of `answers`
    a = jev._parse({"results": {"keep": 0.25, "energy": "steady"}}, questions)
    assert a["keep"].value == 0.25 and a["keep"].confidence == 0.75
    assert a["energy"].value == 2.0

    a = jev._parse({"answers": {"keep": {"value": 1.4}, "energy": {"score": 99}}}, questions)
    assert a["keep"].value == 1.0, "probabilities clamp"
    assert a["energy"].value == 4.0, "positions clamp to the rubric"

    with pytest.raises(jev.JevError, match="did not answer"):
        jev._parse({"answers": {"keep": {"noul": 0.5}}}, questions)
    with pytest.raises(jev.JevError, match="not one of"):
        jev._parse({"answers": {"keep": {"noul": 0.5}, "energy": "nope"}}, questions)


def test_act_routes_on_confidence():
    a = jev.Answer(0.5, 0.95)
    assert a.act(0.9, 0.6) == "act"
    assert jev.Answer(0.5, 0.7).act(0.9, 0.6) == "review"
    assert jev.Answer(0.5, 0.3).act(0.9, 0.6) == "abstain"
    assert jev.Answer(0.5, 0.9).act(0.9, 0.6) == "review", "bounds are exclusive"
    with pytest.raises(ValueError):
        a.act(0.5, 0.9)


async def test_mock_is_deterministic_and_shaped_like_the_real_thing():
    state = {"brief": "x", "transcript_window": [{"id": 1, "text": "hello"}]}
    questions = {
        "s00001_keep": jev.noul("does it belong?"),
        "s00001_energy": jev.score("how energetic?", LEVELS),
        "s00001_role": jev.choice("what role?", {"hook": "opens"}, add_none=False),
    }
    a = await jev.ask(state, questions)
    b = await jev.ask(state, questions)
    assert {k: v.value for k, v in a.items()} == {k: v.value for k, v in b.items()}

    assert 0.0 <= a["s00001_keep"].value <= 1.0
    energy = a["s00001_energy"].value
    assert isinstance(energy, float) and 0.0 <= energy <= len(LEVELS) - 1
    assert a["s00001_role"].value == "hook"

    changed = await jev.ask({**state, "brief": "y"}, questions)
    assert changed["s00001_keep"].value != a["s00001_keep"].value


async def test_mock_refuses_an_unknown_question_type():
    with pytest.raises(jev.JevError, match="no branch"):
        await jev.ask({"s": 1}, {"q": {"type": "vibes", "instructions": "?"}})


async def test_batches_run_concurrently_and_keep_order():
    batches = [({"i": i}, {"q": jev.noul("?")}) for i in range(8)]
    out = await jev.ask_many(batches)
    assert len(out) == 8
    assert [o["q"].value for o in out] == [
        (await jev.ask(s, q))["q"].value for s, q in batches
    ]
    assert await jev.ask_many([]) == []

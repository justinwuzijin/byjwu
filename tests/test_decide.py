"""Tier 2: batching, namespacing, and never spending a request on a regex."""

from __future__ import annotations

import json
from math import ceil

import pytest
from conftest import BRIEF

from cutmcp import decide, jev


@pytest.fixture
def batches(media):
    return decide._batches(media, BRIEF)


def test_trivial_filler_never_appears_in_a_request(media, batches):
    """Gate 2. Not as a question, not as context, not even as text."""
    filler = [s for s in media.segments if s.is_trivial_filler]
    assert filler, "fixture must contain filler for this to prove anything"
    filler_ids = {s.idx for s in filler}

    for state, questions in batches:
        assert filler_ids.isdisjoint(item["id"] for item in state["transcript_window"])
        assert filler_ids.isdisjoint(int(k[1:6]) for k in questions)
    blob = json.dumps(batches, separators=(",", ":"))
    for seg in filler:
        assert f'"id":{seg.idx},' not in blob
        assert json.dumps(seg.text)[1:-1] not in blob


def test_one_request_per_window_of_ten(media, batches):
    """Gate 4. Catches the one-request-per-question regression."""
    judgeable = sum(1 for s in media.segments if not s.is_trivial_filler)
    assert len(batches) == ceil(judgeable / decide.WINDOW)
    assert sum(len(q) for _, q in batches) == judgeable * 5


async def test_decide_issues_exactly_that_many_requests(media, monkeypatch):
    calls: list[int] = []
    real = jev.ask_many

    async def counting(bs):
        calls.append(len(bs))
        return await real(bs)

    monkeypatch.setattr(jev, "ask_many", counting)
    await decide.decide(media, "a different brief, to miss the cache", use_cache=False)

    judgeable = sum(1 for s in media.segments if not s.is_trivial_filler)
    assert calls == [ceil(judgeable / decide.WINDOW)], "one call, one batch per window"


def test_one_decision_per_segment_sorted_by_idx(media, decisions):
    """Gate 3."""
    assert len(decisions) == len(media.segments)
    assert [d.idx for d in decisions] == sorted(d.idx for d in decisions)
    assert [d.idx for d in decisions] == [s.idx for s in media.segments]


def test_question_keys_are_namespaced_per_segment(batches):
    state, questions = batches[0]
    judged = [i["id"] for i in state["transcript_window"] if i["under_judgment"]]
    for idx in judged:
        for name in ("keep", "filler", "energy", "cutq", "role"):
            assert f"s{idx:05d}_{name}" in questions
    assert {q["type"] for q in questions.values()} == {"noul", "score", "choice"}


def test_role_cannot_decline_but_other_choices_could(batches):
    _, questions = batches[0]
    role = next(q for k, q in questions.items() if k.endswith("_role"))
    assert "none" not in role["criteria"], "every line plays some role, even 'dead'"
    assert set(role["criteria"]) == set(decide.ROLES)


def test_windows_carry_context_that_is_not_judged(media, batches):
    assert decide.CONTEXT > 0
    state, _ = batches[1]
    flags = [i["under_judgment"] for i in state["transcript_window"]]
    assert flags.count(True) == decide.WINDOW
    assert flags.count(False) == 2 * decide.CONTEXT
    assert not flags[0] and not flags[-1]


def test_state_only_passes_through_measurements(media, batches):
    """Tier 2 must not compute; every number in the state came from tier 1."""
    by_idx = {s.idx: s for s in media.segments}
    state, _ = batches[0]
    assert set(state) == {"brief", "transcript_window"}
    for item in state["transcript_window"]:
        seg = by_idx[item["id"]]
        assert item["t"] == seg.start
        assert item["pause_before"] == seg.gap_before
        assert item["text"] == seg.text
        assert item["speaker"] == seg.speaker


def test_filler_segments_get_a_certain_synthetic_decision(media, decisions):
    by_idx = {d.idx: d for d in decisions}
    for seg in media.segments:
        if seg.is_trivial_filler:
            d = by_idx[seg.idx]
            assert (d.keep, d.filler, d.role, d.value) == (0.0, 1.0, "dead", 0.0)
            assert d.cutq_conf == 1.0 and d.role_conf == 1.0


def test_value_combines_keep_filler_and_energy():
    top = len(decide.ENERGY_LEVELS) - 1
    base = dict(idx=0, cutq=3, cutq_conf=0.9, role="claim", role_conf=0.9)
    assert decide.Decision(keep=1.0, filler=0.0, energy=top, **base).value == 1.0
    assert decide.Decision(keep=1.0, filler=0.0, energy=0, **base).value == pytest.approx(0.65)
    assert decide.Decision(keep=0.0, filler=0.0, energy=top, **base).value == 0.0
    assert decide.Decision(keep=1.0, filler=1.0, energy=top, **base).value == 0.0
    flat = decide.Decision(keep=0.8, filler=0.1, energy=2, **base)
    assert flat.value == pytest.approx(0.8 * 0.9 * (0.65 + 0.35 * 2 / top))


def test_decisions_are_cached_by_media_and_brief(media, monkeypatch):
    decide.run(media, BRIEF)  # warm

    async def explode(_bs):
        raise AssertionError("cached brief re-asked Jev")

    monkeypatch.setattr(jev, "ask_many", explode)
    assert decide.run(media, BRIEF)

    monkeypatch.undo()
    other = decide.run(media, "an entirely different brief")
    assert [d.value for d in other] != [d.value for d in decide.run(media, BRIEF)]


def test_cost_estimate_is_free_and_plausible(media):
    est = decide.cost_estimate(media, BRIEF)
    judgeable = sum(1 for s in media.segments if not s.is_trivial_filler)
    assert est["segments"] == len(media.segments)
    assert est["requests"] == ceil(judgeable / decide.WINDOW)
    assert est["questions"] == judgeable * 5
    assert est["approx_input_tokens"] > 0
    assert est["approx_usd"] == pytest.approx(
        est["approx_input_tokens"] / 1e6 * jev.USD_PER_M_INPUT_TOKENS, rel=1e-3
    )
    # roughly three cents an hour of footage, per AGENTS.md
    assert est["approx_usd"] < 0.05

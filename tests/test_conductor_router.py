"""Decision router: classification, fallbacks, batching, caching, the call ledger."""

from __future__ import annotations

import json
from fractions import Fraction
from math import ceil
from pathlib import Path

import httpx
import pytest

from conductor import opus
from conductor.candidates import Candidate
from conductor.cli import main
from conductor.errors import ConductorError
from conductor.iterate import iterate
from conductor.router import (
    DECISION_TYPES,
    JEV_WINDOW,
    Ask,
    Router,
    classify,
    register_decision,
    redact,
)
from conductor.run import analyze
from conductor.schema import SchemaError, example, validate, wire

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."
JEV_KEY = "sk-or-v1-routersecret0001"
OPUS_KEY = "sk-ant-api03-routersecret0002"


@pytest.fixture
def env(monkeypatch):
    for name in (
        "OPENROUTER_API_KEY",
        "TYPESAFE_API_KEY",
        "ANTHROPIC_API_KEY",
        "CONDUCTOR_DRY_RUN",
        "CONDUCTOR_JEV_PROVIDER",
        "CONDUCTOR_JEV_MODEL",
        "CONDUCTOR_OPUS_MODEL",
        "CONDUCTOR_OPUS_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(opus, "_BACKOFF", 0.0)
    return monkeypatch


def _no_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(httpx, "Client", boom)


class Jev:
    """A fake Decisions host. Answers every question; can be told to fail."""

    def __init__(self, status: int = 200, body: str | None = None):
        self.status = status
        self.body = body
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append({"headers": dict(request.headers), "body": payload})
        if self.status != 200:
            return httpx.Response(self.status, text=self.body or "upstream down")
        answers = {}
        for key, spec in payload["questions"].items():
            if key.endswith("_action"):
                answers[key] = {"type": "choice", "choice": "remove", "confidence": 0.91}
            elif key.endswith("_risk"):
                answers[key] = {"type": "noul", "noul": 0.1}
            else:
                answers[key] = {"type": "choice", "choice": next(iter(spec["criteria"])), "confidence": 0.88}
        return httpx.Response(
            200,
            json={
                "id": f"gen-{len(self.requests)}",
                "model": "typesafe/jev-1.13-20260917",
                "provider": "TypeSafe",
                "answers": answers,
                "usage": {"input_tokens": 1000, "output_tokens": 0, "cost": 0.000042},
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


class Opus:
    """A fake Anthropic Messages host speaking ``output_config.format``."""

    def __init__(self, status: int = 200, value=None, stop_reason: str = "end_turn", drop: bool = False):
        self.status = status
        self.value = value if value is not None else {"action": "mark_review", "risk": 0.4}
        self.stop_reason = stop_reason
        self.drop = drop
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append({"headers": dict(request.headers), "body": payload})
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error", "error": {"message": "overloaded"}})
        schema = payload["output_config"]["format"]["schema"]
        ids = schema["properties"]["decisions"]["items"]["properties"]["id"]["enum"]
        if self.drop:
            ids = ids[:-1]
        value = self.value
        decisions = [
            {
                "id": item,
                "value": value(item) if callable(value) else value,
                "confidence": 0.72,
                "rationale": "Skin reads cool under the practicals; check it warm.",
            }
            for item in ids
        ]
        return httpx.Response(
            200,
            json={
                "id": f"msg_{len(self.requests)}",
                "model": "claude-opus-5-5",
                "stop_reason": self.stop_reason,
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": json.dumps({"decisions": decisions})},
                ],
                "usage": {"input_tokens": 500, "output_tokens": 80},
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def _gap(index: int, seconds: float = 2.5) -> Candidate:
    start = Fraction(index * 10)
    return Candidate(
        id=f"c{index + 1:04d}",
        kind="silence_gap",
        label="silence gap",
        sequence="Seq",
        clip_id=f"s0c{index}",
        clip_name="Gap",
        role=None,
        timeline_start=start,
        timeline_end=start + Fraction(str(seconds)),
        reason=f"explicit gap of {seconds:.3f}s",
        transcript="",
        signals={"gap_seconds": seconds, "explicit_gap": True, "is_cold_open": False},
        pass_name="mechanical",
    )


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_linear_kinds_go_to_jev_and_creative_to_opus():
    linear = {
        "silence_gap", "short_clip", "filler_pause", "long_static", "colour_role",
        "colour_aspect", "take_keep", "take_compare", "cut_gate", "pacing_violation",
        "subtitle_break", "audio_check",
    }
    creative = {
        "colour_unseen", "story_structure", "key_moments", "music", "typography",
        "visual_treatment", "montage", "broll_selection",
    }
    assert {name for name, item in DECISION_TYPES.items() if item.engine == "jev"} >= linear
    assert {name for name, item in DECISION_TYPES.items() if item.engine == "opus"} >= creative
    for name in linear | creative:
        assert DECISION_TYPES[name].why and DECISION_TYPES[name].question


def test_every_builtin_candidate_kind_has_an_engine():
    from conductor.candidates import KIND_LABEL
    from conductor.colour import _LABEL

    for kind in [*KIND_LABEL, *_LABEL]:
        assert classify(kind).engine in {"jev", "opus"}


def test_xml_only_notes_route_by_what_they_decide():
    for kind in ("covered_gap", "rhythm_shift", "rate_mix", "untrimmed_run", "silent_card"):
        assert classify(kind, "pacing").engine == "jev"
    for kind in ("source_reuse", "music_tail"):
        assert classify(kind, "pacing").engine == "opus"


def test_pass_defaults_and_unknown_kinds():
    assert classify("beat", "story").engine == "opus"
    assert classify("cutaway", "broll").engine == "opus"
    assert classify("breath", "audio").engine == "jev"
    with pytest.raises(ConductorError, match="register_decision"):
        classify("mystery", "mechanical")
    saved = dict(DECISION_TYPES)
    try:
        register_decision("lut_pick", engine="opus", question="Which LUT?", why="A look is taste.")
        assert classify("lut_pick").engine == "opus"
        with pytest.raises(ConductorError, match="engine"):
            register_decision("x", engine="grok", question="q", why="w")
    finally:
        DECISION_TYPES.clear()
        DECISION_TYPES.update(saved)


def test_grok_and_xai_models_are_refused(env):
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    env.setenv("CONDUCTOR_JEV_MODEL", "x-ai/grok-4")
    with pytest.raises(ConductorError, match="Grok/xAI"):
        Router(live=True)
    env.delenv("CONDUCTOR_JEV_MODEL")
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    env.setenv("CONDUCTOR_OPUS_MODEL", "grok-4-fast")
    with pytest.raises(ConductorError, match="Grok/xAI"):
        Router(live=True)


# --------------------------------------------------------------------------
# analyze: attribution and the ledger
# --------------------------------------------------------------------------


def test_dry_run_attributes_every_row_and_counts_calls(env, tmp_path):
    _no_network(env)
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path)
    rows = report.changes + report.payload["kept"]
    assert rows and all(row["engine"] in {"jev", "opus"} for row in rows)
    assert all(row["engine_source"] == "mock" and row["engine_why"] for row in rows)
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["silence_gap"]["engine"] == "jev"
    assert by_kind["colour_unseen"]["engine"] == "opus"
    assert by_kind["colour_unseen"]["rationale"].startswith("dry-run mock")
    usage = report.payload["decision_usage"]["engines"]
    assert usage["jev"]["calls"] == 1 and usage["jev"]["live_calls"] == 0 and usage["jev"]["items"] == 6
    assert usage["opus"]["calls"] == 1 and usage["opus"]["items"] == 1
    assert usage["jev"]["status"] == "mock" and usage["opus"]["status"] == "mock"
    assert report.payload["routing"]["silence_gap"]["engine"] == "jev"
    shadow = report.out_fcpxml.read_text()
    assert "engine=jev/mock | decision=silence_gap" in shadow
    assert "engine=opus/mock | decision=colour_unseen" in shadow
    markdown = report.out_markdown.read_text()
    assert "## Decision engines" in markdown and "| jev | mock | 1 |" in markdown


def test_live_run_uses_both_engines_and_sums_tokens(env, tmp_path):
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    jev_host, opus_host = Jev(), Opus()
    with Router(live=True, jev_client=jev_host.client(), opus_client=opus_host.client()) as router:
        report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path, router=router)
    assert report.mode == "live"
    assert len(jev_host.requests) == 1 and len(opus_host.requests) == 1
    request = opus_host.requests[0]
    assert request["headers"]["x-api-key"] == OPUS_KEY
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    body = request["body"]
    assert body["model"] == "claude-opus-5-5"
    assert "tool_choice" not in body and "thinking" not in body
    assert body["output_config"]["effort"] == "medium"
    wire_schema = body["output_config"]["format"]["schema"]
    assert wire_schema["additionalProperties"] is False
    assert "minimum" not in json.dumps(wire_schema)
    colour = next(row for row in report.changes if row["kind"] == "colour_unseen")
    assert (colour["engine"], colour["engine_source"]) == ("opus", "live")
    assert colour["engine_model"] == "claude-opus-5-5"
    assert colour["confidence"] == 0.72 and colour["disposition"] == "review"
    assert "warm" in colour["rationale"]
    gap = next(row for row in report.changes if row["kind"] == "silence_gap")
    assert (gap["engine"], gap["engine_source"], gap["disposition"]) == ("jev", "live", "auto")
    usage = report.payload["decision_usage"]
    assert usage["engines"]["jev"]["input_tokens"] == 1000
    assert usage["engines"]["jev"]["provider_cost_usd"] == pytest.approx(0.000042)
    assert usage["engines"]["opus"]["input_tokens"] == 500
    assert usage["engines"]["opus"]["output_tokens"] == 80
    assert usage["engines"]["opus"]["estimated_cost_usd"] == pytest.approx(500 * 4e-6 + 80 * 20e-6)
    assert usage["totals"]["live_calls"] == 2
    for path in (report.out_json, report.out_markdown, report.out_fcpxml):
        text = path.read_text()
        assert JEV_KEY not in text and OPUS_KEY not in text


def test_jev_down_falls_back_to_rules_and_never_auto(env, tmp_path):
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    jev_host = Jev(status=503, body=f"bad gateway for Bearer {JEV_KEY}")
    with Router(live=True, jev_client=jev_host.client(), jev_window=2) as router:
        report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path, router=router)
        with pytest.raises(ConductorError, match="no cuts matched"):
            analyze(
                FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "apply",
                router=router, apply=True, min_confidence=0.7, passes=["mechanical"],
            )
    assert len(jev_host.requests) == 3, "one call, its retries, then the breaker holds"
    linear = [row for row in report.changes + report.payload["kept"] if row["engine"] == "jev"]
    assert linear and all(row["engine_source"] == "rules" for row in linear)
    gap = next(row for row in linear if row["kind"] == "silence_gap")
    assert gap["confidence"] == pytest.approx(0.86 * 0.85)
    assert gap["disposition"] == "review"
    assert "Jev unavailable" in gap["engine_detail"]
    assert [row for row in report.changes if row["section"] == "eligible"] == []
    colour = next(row for row in report.changes if row["kind"] == "colour_unseen")
    assert colour["engine_source"] == "unavailable" and colour["disposition"] == "review"
    jev_usage = report.payload["decision_usage"]["engines"]["jev"]
    assert jev_usage["status"] == "down" and jev_usage["failed_calls"] == 1
    assert jev_usage["fallback_items"] == 6
    assert any("Jev unavailable" in text for text in report.warnings)
    dumped = report.out_json.read_text() + report.out_markdown.read_text()
    assert "HTTP 503" in dumped and JEV_KEY not in dumped


def test_rules_are_never_auto_even_with_a_loose_gate(env, tmp_path):
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    taste = tmp_path / "taste.json"
    taste.write_text(json.dumps({"version": 1, "gates": {"auto_confidence": 0.6, "review_confidence": 0.55}}))
    with Router(live=True, opus_client=Opus().client()) as router:
        report = analyze(
            FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "out",
            router=router, taste_path=taste,
        )
    gap = next(row for row in report.changes if row["kind"] == "silence_gap")
    assert gap["engine_source"] == "rules" and gap["confidence"] >= 0.6
    assert gap["disposition"] == "review"


def test_opus_missing_key_leaves_creative_calls_for_review(env, tmp_path):
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    jev_host = Jev()
    with Router(live=True, jev_client=jev_host.client()) as router:
        assert router.status()["opus"].startswith("down")
        report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path, router=router)
    colour = next(row for row in report.changes if row["kind"] == "colour_unseen")
    assert (colour["engine"], colour["engine_source"]) == ("opus", "unavailable")
    assert colour["action"] == "mark_review" and colour["disposition"] == "review"
    assert colour["needs_human"] and not colour["eligible"]
    usage = report.payload["decision_usage"]["engines"]["opus"]
    assert usage["calls"] == 0 and usage["unavailable_items"] == 1
    assert "ANTHROPIC_API_KEY" in usage["down_reason"]
    assert "engine=opus/unavailable" in report.out_fcpxml.read_text()
    gap = next(row for row in report.changes if row["kind"] == "silence_gap")
    assert gap["engine_source"] == "live"


@pytest.mark.parametrize(
    "host",
    [
        Opus(value={"action": "delete", "risk": 0.2}),
        Opus(stop_reason="refusal"),
        Opus(status=529),
        Opus(drop=True),
    ],
    ids=["bad-enum", "refusal", "overloaded", "missing-id"],
)
def test_unusable_opus_answers_become_review(env, tmp_path, host):
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    with Router(live=True, opus_client=host.client()) as router:
        report = analyze(
            FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path, router=router, passes=["colour"]
        )
    unseen = next(row for row in report.changes if row["kind"] == "colour_unseen")
    assert unseen["engine_source"] == "unavailable" and unseen["disposition"] == "review"


def test_live_with_no_engine_key_is_an_error(env):
    with pytest.raises(ConductorError, match="No Jev key"):
        Router(live=True)
    env.setenv("CONDUCTOR_DRY_RUN", "1")
    assert Router(live=True).live is False


# --------------------------------------------------------------------------
# batching and caching
# --------------------------------------------------------------------------


def test_windows_bound_the_call_count(env):
    from conductor.taste import load_taste

    taste = load_taste(None).to_state()
    candidates = [_gap(index, 1.5 + index / 100) for index in range(100)]
    router = Router()
    verdicts, receipts = router.judge_candidates(candidates, brief=BRIEF, taste=taste)
    assert len(verdicts) == 100
    assert router.ledger["jev"].calls == ceil(100 / JEV_WINDOW) == len(receipts)
    assert max(len(item["state"]["candidates"]) for item in receipts) == JEV_WINDOW


def test_identical_regions_are_asked_once(env):
    from conductor.taste import load_taste

    candidates = [_gap(index) for index in range(30)]
    router = Router()
    verdicts, receipts = router.judge_candidates(candidates, brief=BRIEF, taste=load_taste(None).to_state())
    assert len(receipts) == 1 and len(receipts[0]["state"]["candidates"]) == 1
    assert router.ledger["jev"].cache_hits == 29
    assert {verdict.action for verdict in verdicts.values()} == {"remove"}


def test_shared_router_caches_across_runs(env, tmp_path):
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    jev_host, opus_host = Jev(), Opus()
    with Router(live=True, jev_client=jev_host.client(), opus_client=opus_host.client()) as router:
        analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "a", router=router)
        again = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "b", router=router)
        other = analyze(FIXTURE, transcript_path=SRT, brief="A loose cut.", out_dir=tmp_path / "c", router=router)
    assert len(jev_host.requests) == 2 and len(opus_host.requests) == 2
    usage = again.payload["decision_usage"]["engines"]
    assert usage["jev"]["calls"] == 0 and usage["jev"]["cache_hits"] == 6
    assert usage["opus"]["calls"] == 0 and usage["opus"]["cache_hits"] == 1
    assert all(row["cached"] for row in again.changes)
    assert other.payload["decision_usage"]["engines"]["jev"]["calls"] == 1


def test_iterate_totals_and_reuses_unchanged_regions(env, tmp_path):
    result = iterate(fcpxml=FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path)
    assert len(result.rounds) == 2
    first, second = (row["decision_usage"]["engines"]["jev"] for row in result.rounds)
    assert first["calls"] == 1 and second["calls"] == 0 and second["cache_hits"] == second["items"]
    totals = json.loads(result.out_json.read_text())["decision_usage"]
    assert totals["engines"]["jev"]["items"] == first["items"] + second["items"]
    assert totals["totals"]["calls"] == 2


# --------------------------------------------------------------------------
# the generic API the assembly engine uses
# --------------------------------------------------------------------------


def _take_asks():
    return [
        Ask(
            id="t01",
            type="take_compare",
            subject={"a": {"flubs": 2, "peak_db": -1.0}, "b": {"flubs": 0, "peak_db": -6.0}},
            options={"a": "Take A is better.", "b": "Take B is better."},
            question="Fewer flubs wins; a peak above -3 dB loses.",
            rule=lambda ask: ("b", 0.9),
        ),
        Ask(
            id="m01",
            type="music",
            subject={"tracks": ["slow piano", "driving synth"], "section": "cold open"},
            schema={
                "type": "object",
                "properties": {
                    "track": {"type": "string", "enum": ["slow piano", "driving synth"]},
                    "in_seconds": {"type": "number", "minimum": 0},
                },
                "required": ["track", "in_seconds"],
                "additionalProperties": False,
            },
        ),
    ]


def test_generic_decide_dry_run(env):
    _no_network(env)
    decisions, receipts = Router().decide(_take_asks(), brief="teaser")
    take, music = decisions
    assert (take.engine, take.source, take.value, take.confidence) == ("jev", "mock", "b", 0.9)
    assert (music.engine, music.source) == ("opus", "mock")
    assert music.value == {"track": "slow piano", "in_seconds": 0.0}
    assert not take.needs_review and not music.needs_review
    assert [item["engine"] for item in receipts] == ["jev", "opus"]


def test_generic_decide_live_and_fallbacks(env):
    env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    opus_host = Opus(value={"track": "driving synth", "in_seconds": 1.5})
    with Router(live=True, jev_client=Jev().client(), opus_client=opus_host.client()) as router:
        take, music = router.decide(_take_asks())[0]
    assert (take.source, take.value, take.confidence) == ("live", "a", 0.88)
    assert (music.source, music.value["track"]) == ("live", "driving synth")
    assert router.ledger["opus"].live_calls == 1

    with Router(live=True, jev_client=Jev(status=500).client(), opus_client=Opus(status=500).client()) as router:
        asks = _take_asks() + [
            Ask(id="t02", type="take_keep", subject={"flubs": 1}, options={"keep": "k", "cut": "c"})
        ]
        take, music, keep = router.decide(asks)[0]
    assert (take.source, take.value) == ("rules", "b")
    assert take.confidence == pytest.approx(0.9 * 0.85)
    assert keep.source == "unavailable" and keep.needs_review
    assert music.source == "unavailable" and music.needs_review and music.value is None


def test_generic_asks_are_checked(env):
    router = Router()
    with pytest.raises(ConductorError, match="Jev only selects"):
        router.decide([Ask(id="x", type="cut_gate", subject={}, schema={"type": "string"})])
    with pytest.raises(ConductorError, match="over Jev's 250"):
        router.decide([Ask(id="x", type="pacing_violation", subject={}, options={f"o{i}": "" for i in range(251)})])
    with pytest.raises(ConductorError, match="duplicate"):
        router.decide([
            Ask(id="x", type="music", subject={}, options={"a": ""}),
            Ask(id="x", type="music", subject={}, options={"a": ""}),
        ])
    with pytest.raises(ConductorError, match="not one of"):
        router.decide([Ask(id="x", type="cut_gate", subject={}, options={"clean": ""}, rule=lambda a: ("dirty", 1))])


def test_room_run_hands_the_assembler_the_same_router(env, tmp_path):
    import shutil

    from conductor.room import register_assembler, room_run

    seen: list[Router] = []

    def assemble(*, media, music, out_dir, router):
        seen.append(router)
        decisions, _receipts = router.decide(
            [
                Ask(id="track", type="music", subject={"music": [path.name for path in music]},
                    options={path.name: "use this track" for path in music}),
                Ask(id="open_cut", type="cut_gate", subject={"clips_word": False},
                    options={"clean": "lands clean", "clipped": "clips a word"},
                    rule=lambda ask: ("clean", 0.95)),
            ]
        )
        assert [item.engine for item in decisions] == ["opus", "jev"]
        timeline = Path(out_dir) / "assembled.fcpxml"
        shutil.copy(FIXTURE, timeline)
        return timeline

    folder = tmp_path / "selects"
    shutil.copytree("fixtures/selects", folder)
    (folder / "track.mp3").write_bytes(b"ID3")
    register_assembler(assemble)
    try:
        result = room_run(folder, out_root=tmp_path / "out", brief=BRIEF)
    finally:
        register_assembler(None)
    assert len(seen) == 1 and isinstance(seen[0], Router)
    usage = result.payload["decision_usage"]["engines"]
    assert usage["opus"]["items"] >= 2 and usage["jev"]["items"] >= 2
    assert "Decisions: jev" in result.markdown


# --------------------------------------------------------------------------
# schema subset and redaction
# --------------------------------------------------------------------------


def test_schema_validate_wire_and_example():
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["keep", "remove"]},
            "risk": {"type": "number", "minimum": 0, "maximum": 1},
            "note": {"type": "string", "maxLength": 5},
        },
        "required": ["action", "risk"],
        "additionalProperties": False,
    }
    assert validate({"action": "Remove", "risk": 0.2}, schema) == {"action": "remove", "risk": 0.2}
    with pytest.raises(SchemaError, match="above"):
        validate({"action": "keep", "risk": 1.5}, schema)
    with pytest.raises(SchemaError, match="unexpected"):
        validate({"action": "keep", "risk": 0.1, "why": "x"}, schema)
    with pytest.raises(SchemaError, match="longer"):
        validate({"action": "keep", "risk": 0.1, "note": "too long"}, schema)
    with pytest.raises(SchemaError, match="expected number"):
        validate({"action": "keep", "risk": True}, schema)
    sent = wire(schema)
    assert sent["required"] == ["action", "risk", "note"]
    assert "minimum" not in sent["properties"]["risk"] and "maxLength" not in sent["properties"]["note"]
    assert validate(example(schema), schema) == {"action": "keep", "risk": 0.0}


def test_redact_strips_keys(env):
    env.setenv("ANTHROPIC_API_KEY", OPUS_KEY)
    text = redact(f"401 for {OPUS_KEY} and Bearer sk-or-abcdefghijkl")
    assert OPUS_KEY not in text and "abcdefghijkl" not in text


def test_cli_prints_the_call_counter(env, tmp_path, capsys):
    code = main(["analyze", str(FIXTURE), "--brief", BRIEF, "--out-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "decisions jev 1 call" in out and "opus 1 call" in out

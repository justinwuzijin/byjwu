"""Confidence gates and the calibrated mock."""

from __future__ import annotations

import json

import httpx
import pytest

from conductor.errors import ConductorError
from conductor.gates import Gates, route
from conductor.jev import ask, resolve_endpoint
from conductor.taste import load_taste


def test_mechanical_high_confidence_is_auto_and_creative_is_not():
    gates = Gates()
    assert route("remove", 0.86, 0.12, creative=False, gates=gates) == ("remove", "auto")
    assert route("tighten", 0.74, 0.28, creative=False, gates=gates) == ("mark_review", "review")
    assert route("tighten", 0.84, 0.18, creative=True, gates=gates) == ("mark_review", "review")
    assert route("remove", 0.40, 0.10, creative=False, gates=gates) == ("escalate", "escalate")
    assert route("escalate", 0.99, 0.01, creative=False, gates=gates) == ("escalate", "escalate")
    assert route("keep", 0.95, 0.05, creative=False, gates=gates) == ("keep", "keep")
    assert route("remove", 0.90, 0.50, creative=False, gates=gates) == ("mark_review", "review")


def test_mock_silence_and_taste_nudges():
    state = {
        "taste": {"prefs": {"target_pace": "measured", "cold_open_bias": "neutral"}},
        "candidates": [
            {
                "id": "c0001",
                "kind": "silence_gap",
                "duration_seconds": 2.5,
                "signals": {"explicit_gap": True},
            }
        ],
    }
    questions = {
        "c0001_action": {
            "type": "choice",
            "instructions": "pick",
            "criteria": {
                "keep": "k",
                "tighten": "t",
                "remove": "r",
                "mark_review": "m",
                "escalate": "e",
            },
        },
        "c0001_risk": {"type": "noul", "instructions": "risk"},
    }
    batch = ask(state, questions, live=False)
    assert batch.dry_run and batch.model == "conductor-mock-1"
    assert batch.answers["c0001_action"].value == "remove"
    assert batch.answers["c0001_action"].confidence == 0.86
    assert batch.answers["c0001_risk"].value == 0.12

    loose = ask(
        {**state, "taste": {"prefs": {"target_pace": "loose", "cold_open_bias": "neutral"}}},
        questions,
        live=False,
    )
    assert loose.answers["c0001_action"].value == "tighten"
    assert loose.answers["c0001_action"].confidence == 0.70

    cold = {
        "taste": {"prefs": {"cold_open_bias": "keep", "target_pace": "measured"}},
        "candidates": [
            {
                "id": "c0009",
                "kind": "short_clip",
                "duration_seconds": 0.3,
                "signals": {"is_cold_open": True, "flash": False},
            }
        ],
    }
    cold_q = {
        "c0009_action": questions["c0001_action"],
        "c0009_risk": questions["c0001_risk"],
    }
    kept = ask(cold, cold_q, live=False)
    assert kept.answers["c0009_action"].value == "keep"


def test_dry_run_does_not_touch_the_network(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def boom(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(httpx, "Client", boom)
    batch = ask(
        {"candidates": [{"id": "c0001", "kind": "short_clip", "duration_seconds": 0.1, "signals": {"flash": True}}]},
        {
            "c0001_action": {"type": "choice", "criteria": {"remove": "r", "keep": "k", "tighten": "t", "mark_review": "m", "escalate": "e"}},
            "c0001_risk": {"type": "noul", "instructions": "risk"},
        },
        live=False,
    )
    assert batch.answers["c0001_action"].value == "remove"


def test_openrouter_wins_and_posts_the_decisions_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-ts-test")
    monkeypatch.delenv("CONDUCTOR_JEV_PROVIDER", raising=False)
    endpoint = resolve_endpoint()
    assert endpoint.provider == "openrouter"
    assert endpoint.model == "typesafe/jev-1.13"
    assert endpoint.url == "https://openrouter.ai/api/alpha/decisions"
    assert endpoint.headers["Authorization"] == "Bearer sk-or-test"

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-dec-test",
                "model": "typesafe/jev-1.13-20260917",
                "provider": "TypeSafe",
                "answers": {
                    "c0001_action": {
                        "type": "choice",
                        "choice": "remove",
                        "confidence": 0.91,
                        "probabilities": {"remove": 0.91, "keep": 0.09},
                    },
                    "c0001_risk": {"type": "noul", "noul": 0.2},
                },
                "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.0},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    batch = ask(
        {"candidates": []},
        {
            "c0001_action": {
                "type": "choice",
                "criteria": {"remove": "r", "keep": "k"},
            },
            "c0001_risk": {"type": "noul", "instructions": "risk"},
        },
        live=True,
        client=client,
    )
    assert captured["url"].endswith("/api/alpha/decisions")
    assert captured["body"]["model"] == "typesafe/jev-1.13"
    assert batch.model == "typesafe/jev-1.13-20260917"
    assert batch.request_id == "gen-dec-test"
    assert batch.answers["c0001_action"].value == "remove"
    assert batch.dry_run is False


def test_typesafe_uses_the_versioned_model_id(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-ts-test")
    endpoint = resolve_endpoint()
    assert endpoint.provider == "typesafe"
    assert endpoint.model == "jev-1.13.0"
    assert endpoint.url == "https://api.typesafe.ai/v1/systemone"


@pytest.mark.parametrize("slug", ["x-ai/grok-4", "grok-4.7-high", "xai/grok-beta"])
def test_a_grok_model_override_is_allowed(monkeypatch, slug):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("CONDUCTOR_JEV_MODEL", slug)
    assert resolve_endpoint().model == slug


def test_default_models_are_jev_not_grok(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_JEV_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert resolve_endpoint().model == "typesafe/jev-1.13"


def test_live_without_a_key_fails(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_DRY_RUN", raising=False)
    with pytest.raises(ConductorError, match="No Jev key"):
        ask({"candidates": []}, {"c_action": {"type": "choice", "criteria": {"keep": "k"}}}, live=True)


def test_conductor_dry_run_env_blocks_a_live_call(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("CONDUCTOR_DRY_RUN", "1")

    def boom(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(httpx, "Client", boom)
    batch = ask(
        {"candidates": [{"id": "c0001", "kind": "silence_gap", "duration_seconds": 2.5, "signals": {}}]},
        {
            "c0001_action": {"type": "choice", "criteria": {"remove": "r", "keep": "k", "tighten": "t", "mark_review": "m", "escalate": "e"}},
            "c0001_risk": {"type": "noul", "instructions": "r"},
        },
        live=True,
    )
    assert batch.dry_run


def test_fixture_taste_round_trip():
    taste = load_taste("fixtures/taste.json")
    assert taste.prefs["target_pace"] == "measured"
    assert taste.gates.auto_confidence == 0.8
    assert taste.log[0]["event"] == "reject"
    assert taste.to_state()["feedback"]["rejects"] == 1

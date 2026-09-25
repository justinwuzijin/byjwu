"""Opus through the Cursor CLI: backend choice, the child process, parsing, doctor.

``subprocess.run`` is replaced by :class:`FakeCLI`. No test starts cursor-agent.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from conductor import cursor_agent, doctor, opus
from conductor.cli import main
from conductor.errors import ConductorError
from conductor.router import Ask, Router
from conductor.run import analyze

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."
CURSOR_KEY = "cursor-test-key"
HOST_TOKEN = "host-token-placeholder"
JEV_KEY = "test-key"
ANTHROPIC_KEY = "test-key"
MODELS = (
    "Available models\n\n"
    "grok-4.7-low - Grok 4.7\n"
    "grok-4.7-medium - Grok 4.7\n"
    "grok-4.7-high - Grok 4.7\n"
    "claude-opus-5-5-medium - Claude Opus 5.5 1M\n"
    "claude-opus-5-5-high - Claude Opus 5.5 1M\n"
)


class FakeCLI:
    """Stands in for ``subprocess.run``. Answers every item in the prompt's input."""

    def __init__(self, mode: str = "plain", value=None, *, models: str = MODELS, error: Exception | None = None,
                 returncode: int = 0, usage: dict | None = None):
        self.mode = mode
        self.value = value if value is not None else {"action": "mark_review", "risk": 0.3}
        self.models = models
        self.error = error
        self.returncode = returncode
        self.usage = usage
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({
            "argv": list(argv),
            "env": dict(kwargs["env"]),
            "cwd": kwargs["cwd"],
            "timeout": kwargs["timeout"],
            "workspace_files": os.listdir(kwargs["cwd"]),
        })
        if self.error is not None:
            raise self.error
        if argv[1:] == ["models"]:
            return subprocess.CompletedProcess(argv, 0, self.models, "")
        if self.returncode:
            return subprocess.CompletedProcess(argv, self.returncode, "", f"auth failed for {CURSOR_KEY}")
        prompt = argv[-1]
        payload = json.loads(prompt.rsplit("Input:\n\n", 1)[1])
        if "items" in payload:
            answer = {"decisions": [
                {"id": item["id"], "value": self.value, "confidence": 0.66, "rationale": "Hold the reaction."}
                for item in payload["items"]
            ]}
        else:
            answer = {"answer": "pong"}
        text = json.dumps(answer)
        if self.mode == "fenced":
            result = f"Here is the decision.\n```json\n{text}\n```\n"
        elif self.mode == "bad":
            result = "I would keep it warm and let it breathe."
        else:
            result = text
        outer = {
            "type": "result", "subtype": "success", "is_error": False, "result": result,
            "session_id": "session-placeholder", "request_id": "request-placeholder",
            "duration_ms": 1000, "duration_api_ms": 1000,
        }
        if self.usage:
            outer["usage"] = self.usage
        if self.mode == "prose":
            stdout = f"thinking...\n{text}\n"
        elif self.mode == "stream":
            stdout = json.dumps({"type": "system", "subtype": "init"}) + "\n" + json.dumps(outer) + "\n"
        else:
            stdout = json.dumps(outer)
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.fixture
def cursor_env(monkeypatch, tmp_path):
    for name in (
        "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "CONDUCTOR_DRY_RUN",
        "CONDUCTOR_JEV_PROVIDER", "CONDUCTOR_JEV_MODEL", "CONDUCTOR_OPUS_MODEL", "CONDUCTOR_OPUS_EFFORT",
        "CONDUCTOR_TASTE_MODEL", "CONDUCTOR_TASTE_BACKEND", "CONDUCTOR_TASTE_TIMEOUT", "CONDUCTOR_TASTE_CONCURRENCY",
        "CONDUCTOR_OPUS_BACKEND", "CONDUCTOR_OPUS_TIMEOUT", "CURSOR_API_KEY", "CURSOR_AUTH_TOKEN",
        "CURSOR_AGENT_BIN",
    ):
        monkeypatch.delenv(name, raising=False)
    binary = tmp_path / "bin" / "cursor-agent"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o755)
    monkeypatch.setenv("CURSOR_AGENT_BIN", str(binary))
    monkeypatch.setenv("CURSOR_API_KEY", CURSOR_KEY)
    monkeypatch.setenv("CURSOR_AUTH_TOKEN", HOST_TOKEN)
    monkeypatch.setattr(opus, "_BACKOFF", 0.0)
    return monkeypatch


def _install(monkeypatch, fake: FakeCLI) -> FakeCLI:
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _music_asks(count: int) -> list[Ask]:
    return [
        Ask(id=f"m{index:02d}", type="music", subject={"section": index},
            options={"slow piano": "calm", "driving synth": "urgent"})
        for index in range(count)
    ]


def _jev_host(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    answers = {key: {"type": "noul", "noul": 0.97} for key in body["questions"]}
    return httpx.Response(200, json={"id": "gen-1", "model": "typesafe/jev-1.13", "answers": answers})


# --------------------------------------------------------------------------
# backend choice
# --------------------------------------------------------------------------


def test_auto_uses_cursor_for_the_default_slug_and_stops_without_it(cursor_env):
    assert opus.choose_backend() == ("cursor", "")
    cursor_env.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    assert opus.choose_backend() == ("cursor", "")
    cursor_env.delenv("CURSOR_API_KEY")
    backend, why = opus.choose_backend()
    assert backend is None and "CURSOR_API_KEY is unset" in why
    cursor_env.setenv("CONDUCTOR_TASTE_BACKEND", "anthropic")
    assert opus.choose_backend() == ("anthropic", "")


def test_explicit_backends_do_not_fall_through(cursor_env):
    cursor_env.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    cursor_env.setenv("CONDUCTOR_OPUS_BACKEND", "anthropic")
    assert opus.choose_backend() == ("anthropic", "")
    cursor_env.setenv("CONDUCTOR_OPUS_BACKEND", "cursor")
    cursor_env.delenv("CURSOR_API_KEY")
    backend, why = opus.choose_backend()
    assert backend is None and "CURSOR_API_KEY" in why
    cursor_env.setenv("CONDUCTOR_OPUS_BACKEND", "grok")
    with pytest.raises(ConductorError, match="CONDUCTOR_OPUS_BACKEND"):
        opus.choose_backend()


def test_binary_lookup_order(cursor_env, tmp_path):
    pinned = os.environ["CURSOR_AGENT_BIN"]
    assert cursor_agent.find_binary() == pinned
    cursor_env.delenv("CURSOR_AGENT_BIN")
    on_path = tmp_path / "path"
    on_path.mkdir()
    (on_path / "cursor-agent").write_text("#!/bin/sh\n")
    (on_path / "cursor-agent").chmod(0o755)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "cursor-agent").write_text("#!/bin/sh\n")
    (home / ".local" / "bin" / "cursor-agent").chmod(0o755)
    cursor_env.setenv("HOME", str(home))
    cursor_env.setenv("PATH", str(on_path))
    assert cursor_agent.find_binary() == str(on_path / "cursor-agent")
    cursor_env.setenv("PATH", str(tmp_path / "empty"))
    assert cursor_agent.find_binary() == str(home / ".local" / "bin" / "cursor-agent")


# --------------------------------------------------------------------------
# the child process
# --------------------------------------------------------------------------


def test_cursor_backend_runs_read_only_and_attributes_via(cursor_env, tmp_path):
    fake = _install(cursor_env, FakeCLI())
    with Router(live=True) as router:
        assert router.status()["opus"] == "live via cursor"
        report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path / "out", router=router)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    argv = call["argv"]
    assert argv[0] == os.environ["CURSOR_AGENT_BIN"]
    assert argv[1:9] == ["-p", "--mode", "ask", "--trust", "--model", "grok-4.7-medium", "--output-format", "json"]
    assert argv[9] == "--workspace" and argv[10] == call["cwd"]
    assert call["workspace_files"] == [] and not Path(call["cwd"]).exists()
    assert call["timeout"] == 180.0
    assert CURSOR_KEY not in " ".join(argv)
    colour = next(row for row in report.changes if row["kind"] == "colour_unseen")
    assert (colour["engine"], colour["via"], colour["engine_source"]) == ("opus", "cursor", "live")
    assert colour["disposition"] == "review" and colour["confidence"] == 0.66
    assert "engine=opus/live" in report.out_fcpxml.read_text() and "via=cursor" in report.out_fcpxml.read_text()
    usage = report.payload["decision_usage"]["engines"]["opus"]
    assert usage["live_calls"] == 1 and usage["via"] == ["cursor"]
    assert usage["latency_seconds"] is not None and usage["latency_seconds"] >= 0
    assert usage["input_tokens"] is None and usage["estimated_cost_usd"] is None
    assert "via cursor" in report.out_markdown.read_text()
    for path in (report.out_json, report.out_markdown, report.out_fcpxml):
        text = path.read_text()
        assert CURSOR_KEY not in text and HOST_TOKEN not in text


def test_child_env_drops_cursor_auth_token_and_other_keys(cursor_env):
    cursor_env.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    cursor_env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    fake = _install(cursor_env, FakeCLI(value="slow piano"))
    with Router(live=True, jev_client=httpx.Client(transport=httpx.MockTransport(_jev_host))) as router:
        (decision,), _ = router.decide(_music_asks(1))
    assert decision.source == "live"
    env = fake.calls[0]["env"]
    assert "CURSOR_AUTH_TOKEN" not in env
    assert env["CURSOR_API_KEY"] == CURSOR_KEY
    assert "ANTHROPIC_API_KEY" not in env and "OPENROUTER_API_KEY" not in env
    assert os.environ["CURSOR_AUTH_TOKEN"] == HOST_TOKEN, "the host environment is left alone"


def test_real_child_process_never_sees_the_host_token(cursor_env):
    script = Path(os.environ["CURSOR_AGENT_BIN"])
    script.write_text(
        "#!/bin/sh\n"
        '[ -n "$CURSOR_AUTH_TOKEN" ] && exit 3\n'
        '[ "$CURSOR_API_KEY" = "' + CURSOR_KEY + '" ] || exit 4\n'
        'while [ "$1" != "--workspace" ]; do shift; done\n'
        '[ "$2" = "$(pwd)" ] || exit 5\n'
        """printf '%s' '{"type":"result","subtype":"success","is_error":false,"result":"{\\"answer\\":\\"pong\\"}"}'\n"""
    )
    reply = cursor_agent.complete(
        system=doctor.PING_SYSTEM, payload={"ping": True}, schema=doctor.PING_SCHEMA,
        endpoint=cursor_agent.resolve_endpoint(),
    )
    assert reply.data == {"answer": "pong"} and reply.model == "grok-4.7-medium"


def test_cursor_batches_thirty_per_call_and_caches(cursor_env):
    fake = _install(cursor_env, FakeCLI(value="slow piano"))
    with Router(live=True) as router:
        assert router.opus_window == 30 and router.opus_concurrency == 3
        decisions, receipts = router.decide(_music_asks(31))
        again, _ = router.decide(_music_asks(31))
    assert len(fake.calls) == 2
    assert [len(item["state"]["items"]) for item in receipts] == [30, 1]
    assert all(item["via"] == "cursor" for item in receipts)
    assert {item.value for item in decisions} == {"slow piano"}
    assert all(item.via == "cursor" and item.source == "live" for item in decisions)
    assert all(item.cached for item in again)
    assert router.ledger["opus"].cache_hits == 31


@pytest.mark.parametrize("mode", ["fenced", "prose", "stream"])
def test_answer_is_found_inside_other_shapes(cursor_env, mode):
    _install(cursor_env, FakeCLI(mode, value="driving synth"))
    with Router(live=True) as router:
        (decision,), _ = router.decide(_music_asks(1))
    assert (decision.source, decision.value, decision.via) == ("live", "driving synth", "cursor")


def test_reported_tokens_are_counted_but_not_priced(cursor_env):
    _install(cursor_env, FakeCLI(value="slow piano", usage={
        "inputTokens": 10, "outputTokens": 20, "cacheReadTokens": 0, "cacheWriteTokens": 30,
    }))
    with Router(live=True) as router:
        (decision,), _ = router.decide(_music_asks(1))
    assert decision.source == "live"
    row = router.ledger["opus"].to_dict()
    assert (row["input_tokens"], row["output_tokens"]) == (10, 20)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (0, 30)
    assert row["estimated_cost_usd"] is None and row["unpriced_calls"] == 1
    assert row["rate_usd_per_m_tokens"] is None


# --------------------------------------------------------------------------
# failures: every one is a review marker, and the first marks Opus down
# --------------------------------------------------------------------------


def test_bad_json_falls_back_to_review_and_marks_down(cursor_env):
    fake = _install(cursor_env, FakeCLI("bad"))
    with Router(live=True) as router:
        decisions, receipts = router.decide(_music_asks(91))
    assert len(fake.calls) == 3, "the failed wave marks the taste engine down; the next wave is not sent"
    assert all(item.source == "unavailable" and item.needs_review for item in decisions)
    assert "no JSON answer" in router.ledger["opus"].down_reason
    assert receipts[0]["via"] == "cursor" and receipts[0]["source"] == "unavailable"


def test_answer_outside_the_schema_is_refused(cursor_env):
    _install(cursor_env, FakeCLI(value="kazoo solo"))
    with Router(live=True) as router:
        (decision,), _ = router.decide(_music_asks(1))
    assert decision.source == "unavailable"


def test_timeout_becomes_review(cursor_env):
    cursor_env.setenv("CONDUCTOR_OPUS_TIMEOUT", "5")
    fake = _install(cursor_env, FakeCLI(error=subprocess.TimeoutExpired(["cursor-agent"], 5)))
    with Router(live=True) as router:
        (decision,), _ = router.decide(_music_asks(1))
    assert fake.calls[0]["timeout"] == 5.0
    assert decision.source == "unavailable"
    assert "timed out after 5s" in router.ledger["opus"].down_reason


def test_nonzero_exit_is_redacted(cursor_env, tmp_path):
    _install(cursor_env, FakeCLI(returncode=1))
    with Router(live=True) as router:
        report = analyze(FIXTURE, transcript_path=SRT, brief=BRIEF, out_dir=tmp_path, router=router,
                         passes=["colour"])
    unseen = next(row for row in report.changes if row["kind"] == "colour_unseen")
    assert unseen["engine_source"] == "unavailable" and unseen["disposition"] == "review"
    dumped = report.out_json.read_text() + report.out_markdown.read_text()
    assert "exited 1" in dumped and CURSOR_KEY not in dumped


def test_missing_binary(cursor_env, tmp_path):
    cursor_env.setenv("CURSOR_AGENT_BIN", str(tmp_path / "nope" / "cursor-agent"))
    cursor_env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    fake = _install(cursor_env, FakeCLI())
    with Router(live=True, jev_client=httpx.Client(transport=httpx.MockTransport(_jev_host))) as router:
        assert router.status()["opus"].startswith("down") and "not an executable" in router.status()["opus"]
        (decision,), _ = router.decide(_music_asks(1))
    assert decision.source == "unavailable" and fake.calls == []
    cursor_env.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    backend, why = opus.choose_backend()
    assert backend is None and "Cursor CLI" in why


def test_binary_that_vanishes_at_run_time(cursor_env):
    _install(cursor_env, FakeCLI(error=FileNotFoundError(2, "No such file or directory")))
    with Router(live=True) as router:
        (decision,), _ = router.decide(_music_asks(1))
    assert decision.source == "unavailable"
    assert "could not start" in router.ledger["opus"].down_reason


def test_cursor_slugs_are_the_confirmed_families(cursor_env):
    fake = _install(cursor_env, FakeCLI())
    assert cursor_agent.resolve_endpoint().model == "grok-4.7-medium"
    cursor_env.setenv("CONDUCTOR_TASTE_MODEL", "claude-opus-5-5")
    assert cursor_agent.resolve_endpoint().model == "claude-opus-5-5-medium"
    cursor_env.setenv("CONDUCTOR_OPUS_MODEL", "grok-4.7-high-fast")
    cursor_env.delenv("CONDUCTOR_TASTE_MODEL")
    assert cursor_agent.resolve_endpoint().model == "grok-4.7-high-fast"
    for slug in ("grok-4-fast", "xai/some-model", "claude-opus-5-5-thinking"):
        cursor_env.setenv("CONDUCTOR_TASTE_MODEL", slug)
        with pytest.raises(ConductorError, match="refusing"):
            Router(live=True)
    assert fake.calls == []


def test_fixture_output_uses_placeholder_ids():
    sample = json.loads(Path("fixtures/cursor_cli_result.json").read_text())
    assert sample["session_id"] == "session-placeholder"
    assert sample["request_id"] == "request-placeholder"
    reply = cursor_agent.parse(json.dumps(sample), doctor.PING_SCHEMA, model="grok-4.7-medium")
    assert reply.data == {"answer": "pong"}
    assert reply.request_id == "request-placeholder"
    assert reply.usage["input_tokens"] == 10 and reply.usage["cache_write_tokens"] == 30


def test_parse_rejects_an_error_result():
    stdout = json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "quota"})
    with pytest.raises(ConductorError, match="reported an error"):
        cursor_agent.parse(stdout, doctor.PING_SCHEMA, model="claude-opus-5-5")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def test_doctor_reports_both_engines_live(cursor_env):
    cursor_env.setenv("OPENROUTER_API_KEY", JEV_KEY)
    fake = _install(cursor_env, FakeCLI())
    result = doctor.run(jev_client=httpx.Client(transport=httpx.MockTransport(_jev_host)))
    assert result["ok"] is True
    assert result["checks"]["jev"]["live"] and result["checks"]["taste"]["live"]
    assert result["checks"]["taste"]["facts"]["model_listed"] is True
    assert result["taste"]["backend"] == "cursor" and result["taste"]["model"] == "grok-4.7-medium"
    assert [call["argv"][1] for call in fake.calls] == ["models", "-p"]
    assert all("CURSOR_AUTH_TOKEN" not in call["env"] for call in fake.calls)
    text = doctor.format_text(result) + doctor.format_json(result)
    for secret in (CURSOR_KEY, HOST_TOKEN, JEV_KEY):
        assert secret not in text
    text = doctor.format_text(result)
    assert "taste" in text and "grok-4.7-medium" in text and "resolved" in text


def test_doctor_needs_the_slug_in_the_model_list(cursor_env):
    fake = _install(cursor_env, FakeCLI(models="auto\nclaude-opus-5-5-thinking\ngpt-5\n"))
    result = doctor.run()
    row = result["checks"]["taste"]
    assert not row["live"] and "does not list grok-4.7-medium" in row["detail"]
    assert len(fake.calls) == 1, "no round trip when the model is missing"
    assert not result["checks"]["jev"]["live"] and "no key" in result["checks"]["jev"]["detail"]


def test_doctor_cli_exit_code_and_no_secrets(cursor_env, capsys):
    cursor_env.delenv("CURSOR_API_KEY")
    _install(cursor_env, FakeCLI())
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "CURSOR_API_KEY is unset" in out and HOST_TOKEN not in out
    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["taste"]["facts"]["CURSOR_AUTH_TOKEN"].startswith("set on host")
    assert payload["taste"]["model"] == "grok-4.7-medium"

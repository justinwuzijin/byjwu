"""Synthetic fixtures: a 600-segment, ~60-minute interview and a stub media file.

Everything here is seeded, so the fixture transcript is the same on every
machine and the mock judge's answers about it are too.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest

BRIEF = "a 3-minute explainer on why the migration failed"

N_SEGMENTS = 600
SPEAKERS = ("HOST", "GUEST")
TRIVIAL = ("Um.", "Uh.", "Mm.", "You know.", "Uh, um.", "I mean.", "Mhm.")

_VOCAB = (
    "migration database rollback latency schema traffic deploy incident window "
    "team customer replica index throughput cache queue alert dashboard runbook "
    "postmortem shard timeout retry failover budget quarter roadmap tradeoff"
).split()
_OPENERS = (
    "The thing is", "Honestly", "So", "What happened was", "Look",
    "And then", "The short version is", "For context",
)


def _sentence(rng: random.Random, n_words: int) -> str:
    words = [rng.choice(_VOCAB) for _ in range(max(3, n_words))]
    return f"{rng.choice(_OPENERS)}, " + " ".join(words) + "."


def synth_transcript(n: int = N_SEGMENTS, seed: int = 7) -> dict:
    """A plausible diarized interview: runs per speaker, varied pauses, filler."""
    rng = random.Random(seed)
    segments = []
    t = 0.4
    speaker = 0
    turn_left = rng.randint(2, 6)
    for _ in range(n):
        if turn_left == 0:
            speaker = 1 - speaker
            turn_left = rng.randint(2, 6)
        turn_left -= 1

        if rng.random() < 0.08:  # regex-obvious filler
            dur = round(rng.uniform(0.3, 0.9), 2)
            text = rng.choice(TRIVIAL)
        else:
            dur = round(rng.uniform(1.8, 8.5), 2)
            text = _sentence(rng, int(dur * 2.6))
        segments.append(
            {
                "start": round(t, 2),
                "end": round(t + dur, 2),
                "text": text,
                "speaker": SPEAKERS[speaker],
            }
        )
        gap = rng.uniform(0.08, 0.6) if rng.random() < 0.8 else rng.uniform(1.0, 3.2)
        t += dur + gap
    return {"segments": segments}


@pytest.fixture(scope="session", autouse=True)
def _isolated_env(tmp_path_factory):
    """No API key, no shared cache, no cross-test contamination."""
    os.environ["JEV_MOCK"] = "1"
    os.environ["CUTMCP_CACHE"] = str(tmp_path_factory.mktemp("cutmcp-cache"))
    os.environ["HOME"] = str(tmp_path_factory.mktemp("home"))
    os.environ.pop("TYPESAFE_API_KEY", None)
    yield


@pytest.fixture(scope="session")
def media_path(tmp_path_factory) -> Path:
    """A stub media file plus a whisper-shaped JSON sidecar beside it."""
    d = tmp_path_factory.mktemp("footage")
    mp4 = d / "sample.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    (d / "sample.json").write_text(json.dumps(synth_transcript()))
    return mp4


@pytest.fixture(scope="session")
def media(media_path):
    from cutmcp import extract

    return extract.ingest(media_path)


@pytest.fixture(scope="session")
def decisions(media):
    from cutmcp import decide

    return decide.run(media, BRIEF)

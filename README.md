# cutmcp

An MCP server that turns raw interview footage into a cut timeline.

An agent calls five intent-level tools. Underneath, thousands of small typed
judgments are made by [Jev](https://typesafe.ai) — TypeSafe's System One
model — and turned into an EDL by deterministic code.

```
ingest  →  estimate  →  cut  →  review  →  export_timeline
```

The output that matters is not the timeline. It is the ~20 cuts the model was
least sure about, ranked, with the line each one lands after — the list a
human should actually look at.

---

## Install

```bash
pip install mcp httpx numpy          # opentimelineio optional, enables .otio
pip install -e .                     # or just run from the repo
```

Python 3.11+. Footage needs a transcript: install `whisperx` (diarization
gives you speaker labels, which is what makes the interview logic work), or
`whisper`, or drop a whisper JSON sidecar beside the media as
`yourfile.json`.

Jev is early access and waitlisted. Until you have a key, `JEV_MOCK=1` runs
the entire pipeline against a deterministic local judge:

```bash
JEV_MOCK=1 python -c "
from cutmcp import extract, decide, assemble
m = extract.ingest('sample.mp4')
d = decide.run(m, 'a 3-minute explainer on why the migration failed')
t = assemble.build(m, d, 180.0, 'x')
print(len(t.clips), 'clips', round(t.duration, 1), 's')
assert t.duration <= 180, 'budget overrun'
"
```

With a key:

```bash
export TYPESAFE_API_KEY=sk-...       # TYPESAFE_BASE_URL to point elsewhere
export JEV_CONCURRENCY=16            # in-flight requests, default 16
```

## Connect it to an agent

```json
{
  "mcpServers": {
    "cutmcp": {
      "command": "/abs/path/to/venv/bin/cutmcp",
      "env": {
        "TYPESAFE_API_KEY": "sk-...",
        "CUTMCP_CACHE": "/abs/path/to/.cutmcp-cache"
      }
    }
  }
}
```

Use absolute paths for both. A client launches the server from whatever
directory it likes, so a relative `CUTMCP_CACHE` would scatter extracts and
timelines wherever that happened to be — and `review` and `export_timeline`
resolve a `timeline_id` through that cache.

## The five tools

| tool | what it does |
|---|---|
| `ingest(path, force=False)` | transcript, pauses, speakers, loudness. Deterministic, cached, free. Returns a `media_id`. |
| `estimate(media_id, brief)` | what the brief will cost before you spend it |
| `cut(media_id, brief, target_seconds, cut_penalty=0.6)` | the cut. Never overruns the target. |
| `review(timeline_id, limit=20)` | least-confident cuts first |
| `export_timeline(timeline_id, out_dir, fps=24)` | JSON, CMX3600 EDL, and `.otio` when available |

Plus the resource `timeline://{timeline_id}` for the whole thing as JSON.

## The one knob: `cut_penalty`

`cut_penalty` prices every cut in the timeline against how clean that cut
would feel. It is the tightness/smoothness tradeoff, and it is the only
editorial dial — preference is not scattered through prompt text, because a
knob you can slide beats a sentence you have to rewrite.

Measured on the 600-segment, 54-minute test fixture:

| target | `cut_penalty` 0.2 | 0.6 (default) | 1.5 |
|---|---|---|---|
| 60s | 17 clips / 49.9s | 9 clips / 50.4s | 5 clips / 54.1s |
| 180s | 36 clips / 155.3s | 16 clips / 165.4s | 9 clips / 170.3s |
| 600s | 59 clips / 548.3s | 26 clips / 568.4s | 12 clips / 578.7s |

Duration barely moves. What changes is how many cuts you are asking a viewer
to absorb. Low buys density; high buys clips that breathe, at the cost of
carrying weaker lines along.

## What it costs

One request per window of ten segments, five questions each, so 54 minutes of
footage is 55 requests and 2,750 judgments. At $0.042 per million input
tokens with output free, that fixture estimates **$0.0135** — under two cents
an hour of footage. Run `estimate` to price your own before committing.

The estimator's characters-per-token rate is calibrated against usage the API
actually reported, not the usual four-per-token rule of thumb, which
under-counted JSON payloads by 1.8×.

## How it works

```
tier 1  extract.py    deterministic   exact, cached, free
tier 2  decide.py     probabilistic   fuzzy, typed
tier 3  assemble.py   deterministic   exact, reproducible
```

Tiers 1 and 3 never call a model. Tier 2 never measures anything. Jev is
asked only what it is good at — five typed questions per line: does this
belong in a cut serving the brief, is it filler, how energetic is the
delivery, how clean would a cut right after it feel, and what role does it
play. It is never asked for a count, a duration or a timecode, and it emits
no text at all: every answer is a constrained pick from options the code
defines, so it structurally cannot invent one.

Tier 3 then runs a two-state knapsack over the transcript, maximizing value
against the duration budget while pricing every keep-state flip by how clean
that cut was judged to be. Because tiers 1 and 3 are deterministic, **the
same scores always produce the same timeline, byte for byte** — which is what
makes undo, diffable edits, and reproducing a cut six months later possible.
Timeline IDs are blake2b over the inputs, so they resolve across processes.

## Calibration: read this before trusting `review`

The review queue sorts by confidence ascending. That ordering is only
meaningful if Jev is calibrated on footage like yours — if the cuts it calls
at 0.9 really do match a human nine times in ten.

**That is currently unproven.** `eval/calibrate.py` is the harness that
proves or disproves it, against a project you already cut by hand:

```bash
python eval/calibrate.py footage.mp4 "the brief" hand-cut.edl
```

It buckets every call by confidence decile, prints agreement per bucket, and
exits non-zero if any decile is off by more than 15 points. A past project is
a free labeled dataset and better evidence than any vendor benchmark. Until
this passes on your material, treat the review queue order as an assumption.

(The mock judge fails it by construction — its answers are hashes, so
confidence carries no information. That is the harness working.)

## Tests

```bash
JEV_MOCK=1 pytest          # 58 tests, no API key, ~1s
```

The gate that matters: budget never overruns at any target/penalty pair,
`build` is byte-identical across runs, higher `cut_penalty` never increases
clip count, the review queue never silently reorders, EDL timecode
round-trips within a frame, timeline IDs are stable across processes, and no
regex-obvious filler ever reaches a Jev request.

## Known limits

- Single track, A-roll only. No b-roll layering, no multicam.
- The knapsack under-fills by roughly 5–15%, from rounding segment cost up
  and from segments being discrete.
- Transcript sidecars must be whisper/whisperx JSON. No SRT or VTT.
- `lufs` is RMS dBFS, not gated K-weighted loudness — good for comparing
  lines within one recording, not across recordings.
- A brief edit invalidates the whole tier-2 cache.

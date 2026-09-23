# jevid

Two tools share this repo.

| | |
|---|---|
| **Cut Conductor** | Final Cut export in, proposal markers (and, only when you ask, a new cut) out. |
| **cutmcp** | Raw interview footage in, an EDL out. Documented below. |

Cut Conductor is the editorial co-pilot. It does not replace cutmcp.

# Cut Conductor

Decision infrastructure for a Final Cut Pro editor. Export a project as FCPXML, optionally with a transcript, and get back analysis from named passes, typed Jev decisions, a ranked change list, and a **new** FCPXML. The default is a shadow run: proposal markers only. Nothing in v1 rewrites the file you exported, and nothing talks to Final Cut itself.

```text
FCPXML (+ SRT/VTT) → passes → Jev decisions → markers + report
                                              ↘ apply, only with --accept
                                                 or --min-confidence and --pass
```

## How an editor uses it

1. In Final Cut, select the project and choose **File → Export XML…**. Leave the original library alone.
2. Optionally export an SRT or WebVTT whose times match the sequence (not the source clip).
3. From the repo root, dry-run. No API key:

```bash
pip install -e ".[dev]"
python scripts/dry_run.py
```

That runs the checked-in interview fixture and writes `out/sample-dry-run/`. On your own export:

```bash
python -m conductor analyze cut.fcpxml \
  --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-dir out/cut
```

4. Read `cut.conductor.md`. Import `cut.conductor.fcpxml` into a **duplicate event** if you want the markers in Final Cut. Importing creates a new project; it does not patch the one you exported.
5. When a call is actually a cut you want, apply it to yet another file:

```bash
# The one high-confidence mechanical cut (a long silence, in the sample).
python -m conductor apply cut.fcpxml --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --min-confidence 0.8 --pass mechanical --out-dir out/cut

# Or a specific review call you have decided to take.
python -m conductor apply cut.fcpxml --transcript cut.srt \
  --brief "..." --accept c0003 --out-dir out/cut
```

`cut.conductor.applied.fcpxml` is the cut. `cut.fcpxml` is untouched. Creative calls (dialogue, pacing) are not applied by the confidence switch; a person names them with `--accept`.

## Passes

Passes run alone (`--pass mechanical`) or in the default sequence `mechanical`, then `dialogue`, then `pacing`.

| pass | looks for | who may apply it |
|---|---|---|
| `mechanical` | silence gaps of at least **1.25s**, clips shorter than **0.45s** (under **0.20s** is a flash frame) | high-confidence tighten/remove can be applied with `--min-confidence` |
| `dialogue` | a whole filler cue (**0.25–3s**) or a pause of at least **0.80s** between cues | review, unless you `--accept` the id |
| `pacing` | a clip of at least **20s** with under **0.40** words/second. With no transcript, only a hold/slate/b-roll name, or a clip of at least **45s** | review, unless you `--accept` the id |
| `story`, `audio`, `broll` | not implemented | extension point: `conductor.passes.register_pass` |

Filler is the same whole-cue list cutmcp uses (`um`, `you know`, `i mean`, …). `like`, `yeah`, and `okay` are not filler. The numbers live at the top of `conductor/candidates.py`.

A `tighten` on a whole clip keeps the first `hold_seconds` (default 4) and lifts the tail. A `remove`, a filler, or a hole lifts that range and ripples the spine. Transitions on the spine are refused rather than left at a stale offset.

## Confidence gates

Jev's answer is a raw action plus a confidence and a risk. The gate, not the prompt, decides what happens next.

| | default | result |
|---|---|---|
| auto | confidence ≥ **0.80** and risk ≤ **0.35**, and the pass is mechanical | eligible for `--min-confidence` |
| review | confidence ≥ **0.55**, or any creative pass | to-do marker. `--accept` can still cut a raw tighten/remove |
| escalate | confidence below **0.55**, or Jev said escalate | to-do marker, no unattended cut |

Thresholds can be overridden in a taste file under `gates`. The mock judge is calibrated to the heuristics so a dry-run is readable: a 2.5s gap comes back `remove` at 0.86 and clears the auto gate; a filler comes back `tighten` at 0.84 and stays in review because dialogue is creative. That mock is not evidence that live Jev is calibrated. `eval/calibrate.py` is the harness for the other tool in this repo; treat live thresholds as unproven until you have the same kind of check on your own marks.

## Taste

`--taste fixtures/taste.json` loads prefs and a log. The same object is part of the Jev state. Defaults are a no-op for the mock. `target_pace: loose` makes the mock treat a long silence as a review-level tighten. `cold_open_bias: keep` makes the mock keep a candidate that sits on the first spine clip.

```bash
python -m conductor feedback \
  --taste fixtures/taste.json \
  --out out/taste.json \
  --event reject --id c0004 --action tighten --pass dialogue \
  --note "keep the breath"
```

The input taste file is not modified. Schema and the log format are in `fixtures/taste.json` and `conductor/taste.py`. No model is trained from the log. A later room appends accept/reject events; the next decide call sees the counts and the last 20.

## What the run writes

| file | what it is |
|---|---|
| `*.conductor.fcpxml` | the export plus proposal markers. Edits unchanged. |
| `*.conductor.applied.fcpxml` | only after `apply`. Accepted ranges removed, the rest rippled. |
| `*.conductor.json` | the room payload: candidates, gates, receipts, cuts |
| `*.conductor.md` | the ranked list |
| `*.conductor.html` | the same list, one file, no scripts (`--html`) |
| `*.taste.json` | prefs, gates, and the log after this run |

FCPXML markers have no color attribute. Color is the `color=` field of the note (`red` remove, `blue` tighten, `orange` review, `purple` escalate). Review and escalate markers are to-dos (`completed="0"`). A marker's `start` is in the clip's source time, same as the clip's `start`.

## Environment

Copy `.env.example`. Dry-run needs nothing. `--live` calls Jev's Decisions API:

| variable | host | model |
|---|---|---|
| `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` |
| `TYPESAFE_API_KEY` | `POST https://api.typesafe.ai/v1/systemone` | `jev-1.13.0` |

OpenRouter wins when both are set. `CONDUCTOR_JEV_PROVIDER=typesafe` forces the other. `CONDUCTOR_DRY_RUN=1` forces the mock even with `--live`. The TypeSafe host rejects the OpenRouter slug `jev-1.13`; the client sends `jev-1.13.0` there. Both return the same answer shape: a choice with confidence, and a noul with no separate confidence (the probability is the answer).

Jev does not write marker text. The note is assembled in code from the action, the confidence, and the candidate.

## Safety

- Default command is `analyze`. It does not cut.
- `apply` errors unless you pass `--accept` or both `--min-confidence` and `--pass`.
- The confidence path only cuts `auto` dispositions. Creative passes stay review/escalate.
- Output paths that resolve to the source file are refused.
- The source bytes are checked at the end of the run.
- There is no Final Cut plugin and no live timeline control.

## Grok bot room, later

v1 is the library and the CLI those bots will call. Roles, the state payload, and the accept loop are in [`docs/room-protocol.md`](docs/room-protocol.md). A Conductor bot shells out to `python -m conductor analyze` (or imports `conductor.analyze`). It does not get a sixth action, and it does not apply a cut the gate did not allow unless a person accepted that id.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
```

No API key. The conductor tests cover the parser, marker write-back, the mock Decisions client, the gates, and apply. The cutmcp tests are unchanged.

---

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

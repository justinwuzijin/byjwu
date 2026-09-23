# AGENTS.md — cutmcp

Context for coding agents working in this repo. Read this before editing.
Works as-is for Cursor and Codex; `cp AGENTS.md CLAUDE.md` for Claude Code.

## What this is

An MCP server that turns raw interview footage into a cut timeline. An agent
calls five intent-level tools; underneath, thousands of small typed judgments
are made by Jev (TypeSafe's System One model) and turned into an EDL by
deterministic code.

Human-facing setup lives in `README.md`. This file is about **how to change
the code without breaking the design**.

---

## The tier rule

This is the one invariant. Everything else follows from it.

```
tier 1  extract.py    deterministic      exact, cached, free
tier 2  decide.py     probabilistic      fuzzy, typed, ~3¢ per hour of footage
tier 3  assemble.py   deterministic      exact, reproducible
```

**Tiers 1 and 3 must never call a model. Tier 2 must never compute anything.**

If you find yourself wanting a count, a duration, a timecode, a sum or a date
inside `decide.py`, it belongs in tier 1 or tier 3. Jev is not a calculator.
If you find yourself wanting a judgment inside `assemble.py`, it belongs in
tier 2 and must arrive as a score on a `Decision`.

The payoff is that **the same scores always produce the same timeline**, byte
for byte. That gives real undo semantics, diffable edits, and a cut you can
reproduce six months later. Any change that breaks determinism in tier 1 or 3
breaks the product, not just a test.

---

## File map

| file | holds | never holds |
|---|---|---|
| `jev.py` | API client, question constructors, mock judge | anything about video |
| `extract.py` | transcript, pauses, speakers, loudness, regex filler | anything fuzzy |
| `decide.py` | the five questions, batching, `Decision.value` | arithmetic, I/O, export |
| `assemble.py` | knapsack DP, review queue, EDL/OTIO emit | model calls |
| `server.py` | MCP tool surface only | logic of any kind |

`server.py` is a thin shell on purpose. If a tool function grows past ~15
lines, the logic belongs in a tier module.

---

## Jev reference

Reconstructed from launch materials and third-party MCP servers, **not from
official docs**. Verify at `docs.typesafe.ai/api`.

Three primitives, all in `jev.py`:

- `noul(instructions)` → probability a condition holds, `value` in [0,1]
- `score(instructions, levels)` → index into ordered `levels`
- `choice(instructions, options)` → one key from `options`

Every answer carries `confidence`. `Answer.act(act_above, review_above)`
returns `act` / `review` / `abstain` — use that rather than hand-rolling
thresholds.

Facts that constrain design:

- **Jev generates no text, code or rationale.** It only selects from options
  you define. This is a feature: it structurally cannot invent something that
  wasn't in the option set. Never design a flow that needs it to write.
- **250 options max** per choice (`jev.MAX_OPTIONS`). Above that, chunk and
  re-rank the winners.
- **~70–500ms** per request, **$0.042/M input tokens**, output free.
- Budget roughly 64k tokens for state + questions combined.
- Early access, waitlisted. `JEV_MOCK=1` runs a deterministic local judge so
  the whole pipeline works without a key. **Keep mock mode working** — every
  new question type needs a mock branch in `jev._mock`.

`jev.py` is the only file coupled to the API contract. If the shape is wrong,
fix it there and nothing else should need to change.

---

## Where taste lives

Exactly two places. Keep it that way.

1. **`Decision.value`** in `decide.py` — combines keep probability, filler
   probability and delivery energy into one scalar the DP maximizes.
2. **`cut_penalty`** — the `cut()` tool argument, priced against cut quality
   at every keep-state flip in the DP.

Measured behavior on a 60-min interview cut to 180s:

| `cut_penalty` | clips | duration |
|---|---|---|
| 0.2 | 34 | 160.4s |
| 0.6 (default) | 23 | 163.3s |
| 1.5 | 17 | 165.8s |

Do **not** add editorial preference as prompt text scattered through the
questions. A knob the user can slide beats a sentence they have to rewrite.

---

## Hard rules

1. Don't ask Jev what code can compute. Regex-obvious filler is caught in
   `extract.FILLERS` and never reaches the model.
2. Batch. One request per window of `WINDOW=10` segments with namespaced keys
   (`s00042_keep`). Never loop one question per request — per-request overhead
   dominates and 50 questions cost barely more than one.
3. New signals go in tier 1 as structured fields, not as harder questions.
   Jev judges better on good structured state than on clever instructions.
4. Never let the review queue silently reorder. It sorts by confidence
   ascending, which is only meaningful if the model is calibrated.
5. Overshooting a duration target is a real failure; undershooting by a
   quantum is not. Segment cost rounds **up** and includes `gap_before`,
   because a kept run spans the pauses inside it.
6. `choice()` adds a `none` option by default so the model can decline.
   Only pass `add_none=False` when one option must always apply.

---

## Running and verifying

```bash
pip install mcp httpx numpy            # opentimelineio optional, enables .otio
JEV_MOCK=1 python -c "
from cutmcp import extract, decide, assemble
m = extract.ingest('sample.mp4')
d = decide.run(m, 'a 3-minute explainer')
t = assemble.build(m, d, 180.0, 'x')
print(len(t.clips), 'clips', round(t.duration,1), 's')
assert t.duration <= 180, 'budget overrun'
"
```

Footage needs a transcript: install `whisperx`, or drop a whisper JSON beside
the media as `yourfile.json`. Diarization matters — `Segment.speaker` is what
makes interview logic work.

MCP SDK is **2.x**, where `FastMCP` was renamed `MCPServer`. `server.py`
targets 2.x; the v1 import is noted in a comment there.

---

## Open work, roughly in order

**1. Calibration harness.** The highest-value missing piece. Take a project
already cut by hand, run `cut()` against the same brief, and bucket cuts by
confidence decile — of the ones called at 0.9, did ~90% match the human
decision? Past projects are a free labeled dataset and better evidence than
any vendor benchmark. Until this exists, treat the review queue ordering as
unproven. Put it in `eval/`.

**2. B-roll layering.** A second `Choice` pass in `decide.py`: for each kept
A-roll line, choose among ≤250 candidate clips (pre-filter with embeddings to
get under the cap). Emit as a second track in `assemble.export`. Reuse the
existing window batching.

**3. Multicam angle selection.** A per-segment `Choice` over available angles.
Slots into `_questions()` with no structural change.

**4. Tighten the knapsack.** Currently under-fills by ~6–8% (553s against a
600s target) from ceil rounding plus discrete segments. Dropping `quantum` to
0.1 fixes it at ~6× DP cost. A better fix is a post-pass that extends the
highest-value clips into leftover budget.

**5. Incremental re-decide.** Tier 2 caches per `(media_id, brief)`. A brief
edit currently invalidates everything. Segment-level caching keyed on segment
text plus question text would make brief edits nearly free.

## Known rough edges

- Single track, A-roll only.
- `_load_sidecar` accepts whisper/whisperx JSON only; no SRT/VTT.
- Timeline IDs use `hash()`, which is not stable across processes. Fine for
  cache lookup within a session, wrong if you ever need a durable ID —
  switch to blake2b over the same inputs.

---

## Cut Conductor (`conductor/`)

A second package in this repo. It is an FCPXML co-pilot: named passes,
Jev decisions, proposal markers, and an explicit apply that writes a new
file. Human docs are the Cut Conductor section of `README.md` and
`docs/room-protocol.md`.

It does **not** follow the tier rule above, and it is not a sixth MCP tool.
Do not fold its pipeline into `cutmcp/decide.py` or `cutmcp/assemble.py`.
Do not route its Jev calls through `cutmcp/jev.py`'s `ask` — the Decisions
client, the mock, and the action set live in `conductor/jev.py`. The one
shared piece is `cutmcp.jev.Answer`, `choice`, and `noul` as question
constructors, plus `cutmcp.extract.is_trivial_filler` so both tools agree
on what a whole-cue filler is.

`JEV_MOCK=1` is the cutmcp switch. Conductor ignores it. Conductor dry-run
is the default; `CONDUCTOR_DRY_RUN=1` forces the mock even with `--live`.

Passes (`mechanical`, `dialogue`, `pacing`, plus reserved `story` / `audio`
/ `broll`) are the extension point. A new editorial check is a
`register_pass`, not a new CLI. Confidence gates live in `conductor/gates.py`.
Creative passes never take the `auto` disposition. Apply never overwrites
the input FCPXML.

# AGENTS.md

The product in this repo is **byjwu** (the engine was once called Cut
Conductor). It edits Justin's (@byjustinwu) YouTube videos: raw footage and
music in, a finished FCPXML out that he opens in Final Cut Pro himself. Jev
makes bounded, logical decisions. Claude Opus 5.5 makes open-ended taste
decisions and builds graphics through code. A Grok Bot room coordinates, and
no Grok model makes editorial decisions. Human docs are `README.md` (short),
`docs/technical.md`, and `docs/room-protocol.md`. The package you edit for that product is
`conductor/`. Do not rename it, `python -m conductor`, or the console
scripts. Other work depends on those names. The drop folders are
`~/Desktop/byjwu-in` / `byjwu-out` (`conductor/folders.py`), with a fallback
to the legacy folder names on machines set up before the rename.

What follows is the design rule for **cutmcp**, the older raw-footage MCP
cutter that still lives here. `conductor/` does not follow that tier rule.
Read the byjwu engine section at the bottom of this file before
changing Final Cut behavior.

Works as-is for Cursor and Codex; `cp AGENTS.md CLAUDE.md` for Claude Code.

## What cutmcp is

An MCP server that turns raw interview footage into a cut timeline. An agent
calls five intent-level tools; underneath, thousands of small typed judgments
are made by Jev (TypeSafe's System One model) and turned into an EDL by
deterministic code.

Human-facing setup for cutmcp lives in `docs/cutmcp.md`. The product README
is `README.md`, and its technical docs are `docs/technical.md`. This file is about **how to change the code without breaking
the design**.

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

## byjwu engine (`conductor/`)

A second package in this repo, historically called Cut Conductor (Cut
Conductor is now the room bot that runs it). It is an FCPXML co-pilot: named passes,
Jev decisions, proposal markers, and an explicit apply that writes a new
file. Human docs are `docs/technical.md` and `docs/room-protocol.md`.

`python -m conductor ingest` inventories a folder of clips, writes a starter
FCPXML (filename order, absolute `file://` paths), and calls `analyze`.
`--apply` uses the same gates as `apply`. Source media is only read.
Durations come from a `--durations` map, otherwise ffprobe, otherwise a 10s
placeholder when the file cannot be probed. `python -m conductor ui` is a
localhost page that posts a folder path to that command. It does not upload
media.

The engine does **not** follow the tier rule above, and it is not a sixth MCP tool.
Do not fold its pipeline into `cutmcp/decide.py` or `cutmcp/assemble.py`.
Do not route its Jev calls through `cutmcp/jev.py`'s `ask` — the Decisions
client, the mock, and the action set live in `conductor/jev.py`. The one
shared piece is `cutmcp.jev.Answer`, `choice`, and `noul` as question
constructors, plus `cutmcp.extract.is_trivial_filler` so both tools agree
on what a whole-cue filler is.

`JEV_MOCK=1` is the cutmcp switch. Conductor ignores it. Conductor dry-run
is the default; `CONDUCTOR_DRY_RUN=1` forces the mock even with `--live`.

### Decision routing (`conductor/router.py`)

Product rule: a linear, logical decision (a bounded choice with clear
criteria) must call **Jev**. An open-ended creative or taste decision goes
to **Claude Opus 5.5** (`conductor/opus.py`, `ANTHROPIC_API_KEY`, model
`claude-opus-5-5`, `CONDUCTOR_OPUS_MODEL` to change it). **No Grok or xAI
model anywhere in the decision path.** `jev.refuse_xai` enforces that on
every model id and URL. Room bots orchestrate. They do not decide.

- The classification is `router.DECISION_TYPES`. Each type has an engine, a
  question, and a reason. Add a type with `register_decision`. Do not branch
  on engine anywhere else. Candidate kinds map to a type of the same name.
  An unknown kind in a reserved pass uses `PASS_DEFAULTS`, and anything
  else raises.
- Everything that decides goes through `Router`: `judge_candidates` for
  pass candidates, `decide([Ask(...)])` for everything else (the assembly
  engine, future passes). Do not call `jev.ask` or `opus.complete` directly
  from a pass.
- Jev asks carry `options` (≤250) and never a schema. Jev selects, it does
  not write. Give a linear ask a `rule`: it is the dry-run answer and the
  fallback.
- Opus asks carry `options` or a JSON `schema`. The wire schema is stripped
  to what Anthropic accepts (`schema.wire`). The answer is validated against
  the full schema (`schema.validate`). Opus 5.5 rejects forced `tool_choice`
  and disabled thinking. Use `output_config.format`.
- Fallbacks are not negotiable. If Jev is down, the linear call goes to the
  deterministic rules at `FALLBACK_DISCOUNT` (0.85×), never to an LLM, and
  a rules answer is never `auto`. If
  Opus is down, the creative call becomes a review marker with no action.
  The first failed request marks that engine down for the rest of the run.
- The gate (`gates.route`) is separate from the engine. A Jev call on a
  creative pass is still review-only. Threshold comparisons in the gate are
  arithmetic. They stay code.
- Batching: `JEV_WINDOW` candidates per Jev request, `OPUS_WINDOW` per Opus
  request, split at `MAX_STATE_CHARS`. The cache is keyed by content (no id,
  no timeline position), plus brief, taste prefs, question, engine, model,
  and live-vs-mock. `iterate` shares one router across rounds.
- Attribution: every `Proposal` carries `engine`, `engine_source` (`live`,
  `mock`, `rules`, `unavailable`), `decision_type`, `engine_why`, and
  `cached`. Report rows and marker notes show it. `decision_usage` in the
  room payload (and totals in `iterate.json`) is the call counter. Keep both
  when you change the payload.
- Errors that reach a report go through `router.redact`. Never log a key.
- Tests use `httpx.MockTransport` hosts (`tests/test_conductor_router.py`).
  Dry-run must work with no key and no network.

`conductor.graphics.apply_graphics` is the type and graphics stage (`iterate`,
`room-run`, and `assemble` call it). It is off unless `--graphics` is passed
or the profile sets `graphics.enabled`. Subtitle breaks, timing, and partial
words are Jev asks. Title placement and treatment are Opus asks. The
placeholder type defaults live in `conductor/graphics/profile.py` and are not
measurements. Rendered media goes in `<stem>.assets/` beside the output
FCPXML. Missing ffmpeg or Pillow skips that render and records a note.

Passes (`mechanical`, `dialogue`, `pacing`, `colour`, plus reserved `story` /
`audio` / `broll`) are the extension point. A new editorial check is a
`register_pass`, not a new CLI. `colour` is review-only: it reads roles and
aspect from the XML, leaves a placeholder where exposure and skin would need
a decode, and never auto-applies. `python -m conductor room-run` is the one command a room bot runs. It
detects a `.fcpxml`, `.fcpxmld`, zip, or clip folder, calls `iterate`, and
writes a timestamped folder plus `room.md`. `python -m conductor iterate` is
the loop that command calls: each round auto-applies only mechanical gate cuts, writes
`out/vN/`, and stops on metrics, no progress, or `--max-rounds`. Confidence
gates live in `conductor/gates.py`. Creative passes never take the `auto`
disposition. Apply never overwrites the input FCPXML.

XML-only signals, for a timeline with no transcript, live in
`conductor/candidates.py`. A spine gap counts as silence only where no
connected clip covers it, and `apply` will not remove a spine item wholesale
while a connected clip still covers part of it. Holds are judged against the
local average shot. Repeated source ranges, rhythm shifts, mixed frame rates,
an untrimmed string-out, a silent generator card, and a music bed that ends
early are pacing notes (`span="note"`); the mock will not lift them. Each of
those kinds is a registered decision type in `conductor/router.py`. The
lane-less media inside a compound `clip` is the clip's picture, not an
anchored item. A connected item's `offset` is on the parent's clock, which
begins at the parent's `start`. The parser, apply, and the media path all
place connected items, items on a gap, secondary storylines, and disabled
clips with `timing.anchor_time`; hand-built fixtures must follow the same
rule. Markers are inserted before `audio-channel-source` and filter
elements, which is where the 1.14 content model requires them.

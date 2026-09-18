# DECISIONS

Choices made where the build spec was silent, and the four places I departed
from it. Everything here is reversible; nothing here is load-bearing to the
tier rule.

## Departures from the spec

**1. The budget axis floors instead of rounding.** The spec says
`B = round(target/quantum) + 1`. When `target/quantum` is not an integer,
rounding goes *up*, and the optimum can then overrun the target by most of a
quantum — which acceptance gate 5 forbids and which AGENTS.md calls a real
failure. `B = floor(target/quantum) + 1` is identical for the common case
(any target that is a whole number of quanta, including 60/180/600 at 0.25)
and strictly safe otherwise. Undershooting by a quantum is not a failure;
overshooting is.

**2. Segment cost rounds up, but only after an epsilon.**
`ceil((duration + gap_before) / quantum)` on raw floats charges an extra
quantum whenever the division lands on `20.000000000000004`. Rounding the
quotient to nine decimals before the ceiling kills the float noise without
weakening the round-up. This matters for determinism, not just fill: the
same input must cost the same every time on every machine.

**3. `gap_before` of the first segment is 0.0, not `start`.** Nothing
precedes segment 0, and since cost includes `gap_before`, defining it as the
lead-in would charge a file with a 60-second slate 60 seconds to use its
first line — effectively banning it from every cut. `gap_after` of the last
segment *is* the tail silence, which is informational and unpriced.

**4. `decide.py` contains two formulas.** AGENTS.md says tier 2 holds no
arithmetic; the build spec puts both `Decision.value` and `cost_estimate`
there. I followed the build spec. The invariant that actually matters is
that tier 2 never *measures* — no counts, durations, timecodes or dates —
and it does not: every number in a request state was measured by tier 1 and
is passed through untouched (`test_state_only_passes_through_measurements`
pins this). `value` is a pure function of model outputs and `cost_estimate`
prices payloads that tier 2 itself constructs.

## Tier 1 — extract

- **`FILLERS` is deliberately short.** Only tokens that carry no content in
  any context: `um`, `uh`, `er`, `mm`, `hmm`, `you know`, `i mean` and
  spelling variants. `like`, `right`, `yeah` and `okay` are *not* included
  even as whole segments — they are frequently load-bearing in an interview,
  and deleting them with a regex is a judgment a pattern should not be
  making. They reach the `filler` noul in tier 2 instead. A filler match
  requires the *entire* normalized line to be filler.
- **`lufs` is RMS dBFS, not gated K-weighted LUFS.** One ffmpeg pass decodes
  to 8 kHz mono PCM and numpy takes per-segment RMS. It is a relative
  delivery signal — a mumbled aside versus a landed line in the same
  recording — and true LUFS would not change any decision it feeds. The
  field keeps the spec's name. When ffmpeg is absent or the decode fails,
  `lufs` is `None`, which downstream treats as unknown, never as quiet.
- **`words` is a count**, taken from whisper's word timings when the
  transcript has them and from whitespace tokens otherwise. Rate of speech
  is the signal worth having; the timings themselves are not used yet.
- Segments are sorted by start time and re-indexed on ingest, and
  empty-text segments are dropped, so `idx` is always dense and ordered.
- Subprocess calls (ffprobe, ffmpeg, whisper) get `stdin=DEVNULL` so a tool
  that decides to prompt can never wedge the pipeline.
- Cache location is `CUTMCP_CACHE`, defaulting to `./.cutmcp-cache`. Tests
  point it at a tmpdir.

## Tier 2 — decide

- **Context excludes filler too.** Gate 2 says filler segments never appear
  in a request; windows and their `CONTEXT` neighbours are both drawn from
  the filler-free list, so filler is absent as a question, as context, and
  as text.
- **Synthetic verdict for trivial filler**: `keep=0, filler=1, role="dead",
  energy="flat"`, and `cutq="clean"` — cutting straight after a bare "um"
  genuinely is clean. All confidences are 1.0, which also parks these at the
  bottom of the review queue, where they belong.
- **The tier-2 cache key includes the mock flag**, a schema version and
  `WINDOW`, not just `(media_id, brief)`. Without that, a run under
  `JEV_MOCK=1` would silently poison the cache for the first run with a real
  key, and a reworded question would reuse answers to the old one.
- **Question instructions name the line by id** (`"Line 42: ..."`) so
  namespaced keys and the state array cannot drift apart.
- `cost_estimate` measures the exact payloads `decide` would send at four
  characters per token. On the test fixture that is $0.0076 for 54 minutes,
  rather than the ~3¢/hour quoted in AGENTS.md; the estimator reports what
  it measures, and denser footage will cost more.

## Tier 3 — assemble

- **Tie-breaks prefer fewer cuts, always deterministically.** Where two DP
  paths score equally, the one that does not flip keep-state wins, and the
  final `argmax` resolves to the lowest budget with the previous segment
  dropped. Ties are otherwise a source of nondeterminism, and this direction
  also strengthens the monotonicity the gate checks.
- **One `Cut` per run boundary, including the last.** Each carries the
  quality label and confidence of the line it lands after, plus that line's
  tail and the head of the next one, so a cut can be judged in the review
  queue without opening an NLE. The trailing cut has an empty `next_text`.
- **Timelines persist** to `.cutmcp-cache/{timeline_id}.timeline.json`, which
  is what lets `review` and `export_timeline` take an id from a previous
  session. It is also why the id has to be blake2b rather than `hash()`.
- **The timeline id hashes** media id, brief, target, quantum, cut penalty
  and the kept indices — everything that determines the cut and nothing
  that does not. Timelines carry no timestamps, so two builds of the same
  input are byte-identical.
- **EDL**: reel `AX`, channel `AA/V`, non-drop frame. Record timecode
  accumulates in *frames* rather than seconds so the record track can never
  drift from the source durations. Segment ranges go in a comment line.
- `select` raises `KeyError` when a segment has no `Decision` rather than
  quietly skipping it — a missing judgment is a bug, not a drop.

## jev.py — contract verified against the live API

The spec said the wire format was reconstructed from launch materials rather
than documented, and it was wrong. TypeSafe publishes an OpenAPI schema at
`https://api.typesafe.ai/openapi.json` (TypeSafe 0.2.0, model `jev-1.13.0`
behind the `jev-latest` alias); `jev.py` now matches it, confirmed with a
live request. Three corrections:

- **Options live under `criteria`, not `levels` or `options`**, and the
  field is required for score and choice. A noul takes an optional
  `criteria: {true, false}` spelling out what each verdict means.
- **A noul returns no confidence at all** — `{"type":"noul","noul":0.98}`.
  The probability is the whole answer, so `Answer.confidence` is filled in
  as `max(p, 1-p)`, the model's implied odds that its own call is right.
  The reconstruction had guessed this fallback; it turns out to be the only
  behaviour, which is also what makes the calibration harness's folded
  confidence the natural metric.
- **A score returns a float, not an index**: the probability-weighted mean
  of the rubric, so 1.7 is a real answer. `Decision.energy` and
  `Decision.cutq` are floats accordingly, which the DP prefers anyway —
  `flip` gets continuous resolution instead of five steps. `cut_quality`
  rounds to the nearest label only for display. Score `probabilities` come
  keyed by level index and are relabelled to level names on the way in.

Parsing stayed forgiving (`answers` or `results`, bare scalars, labels where
an index is expected) because the cost of that is a few lines and the cost
of being wrong again is the whole pipeline. `tests/test_jev.py` pins the
documented shapes as fixtures — an addition to the spec's file tree, on the
grounds that the one module coupled to an external contract should fail
first and loudly when that contract moves.

`cost_estimate`'s four-characters-per-token rule was also wrong: a real
2,374-character payload billed 1,059 input tokens, so the rate is now 2.25.
JSON tokenizes much denser than prose, and the old constant under-reported
spend by 1.8×, which is the wrong direction for a number you decide on.

## jev.py — other choices

- `Answer.act` treats both bounds as exclusive, matching the `_above` names.
- A bare float for a noul yields `confidence = max(p, 1-p)`: the model's
  implied probability that its own call is right. Inventing 1.0 there would
  push unexamined cuts to the bottom of the review queue.
- `choice()` accepts a mapping of key → description or a plain sequence, and
  enforces `MAX_OPTIONS` *after* injecting `none`.
- `ask_many` shares one `AsyncClient` across the whole fan-out; `ask` opens
  one only if it was not handed a client.
- Three retries with exponential backoff on 408/429/5xx and on transport
  errors; other statuses fail fast.
- The mock judge spreads confidence over [0.55, 1.0) rather than emitting a
  constant, so the review queue has a real ordering to exercise in tests.

## eval/calibrate.py

- **Confidence is folded onto the call, not read off `p(keep)` raw.** The
  obvious version — bucket by `p(keep)`, compare against "the human kept
  this line" — cannot pass even with a perfect model: "belongs in a cut" and
  "survived into a 168-second cut" are different events, and the base rates
  differ by an order of magnitude. Instead each line contributes the call
  the pipeline actually made (kept or dropped) and the model's own stated
  probability that the call is right (`p` if kept, `1-p` if not). Both
  populations are then the same size, and the question the spec asks — of
  the calls made at 0.9, did ~90% match the human? — is well posed. It also
  handles budget-forced drops honestly: a line scored 0.9 and dropped for
  budget lands in the 0.1 bucket, where it should be wrong most of the time.
- **Deciles thinner than `--min-bucket` (default 10) are printed but not
  gated.** A bucket of two is noise and should not fail a build.
- The target defaults to the duration of the hand cut, so the comparison is
  between two cuts of the same length.
- A human line counts as kept when the EDL covers at least half of it.

## Tests and toolchain

- The 600-segment fixture is seeded, so the transcript and therefore every
  mock answer about it is identical on every machine.
- Gate 1 proves a cache hit by monkeypatching both transcript readers to
  raise; gate 10 proves cross-process stability by re-running the pipeline
  in a subprocess with a different `PYTHONHASHSEED` and the tier-2 cache
  disabled, so the mock judge's determinism is under test too, alongside the
  hardcoded id constants.
- `test_tools_stay_thin` asserts every MCP tool body is ≤15 lines. It fails
  the moment logic leaks out of a tier module.
- The exported EDL is parsed back by the eval harness's own reader in a
  test, so the writer and the calibration reader cannot drift apart.
- MCP SDK 2.2.0 verified: `mcp.server.mcpserver.MCPServer` exists as
  specified. Two details the spec could not have known — the decorators
  return the undecorated function (so tests call `server.cut(...)`
  directly), and the resource template field is `uri_template`.
- The system interpreter here is Python 3.9, below the 3.11 floor, so the
  toolchain lives in a gitignored `.venv` built by `uv` with CPython 3.12.
  Nothing in the package depends on it.

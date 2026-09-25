# Room protocol

How the byjwu-editor Grok Bot room drives the editing engine (the `conductor` package, `python -m conductor`). The repo holds the engine and the CLI. No bot is implemented here, and nothing in this repo talks to Final Cut or to a bot API. This file is the contract those bots call.

The bots coordinate. They do not make editorial decisions. Bounded, logical calls come from Jev (`conductor/jev.py`). Open-ended creative and taste calls come from Claude Opus 5.5 through the Jev/Opus decision router, which is in progress. No Grok model makes an editing decision.

Justin, the owner, does not use the command line. He drops footage, music, or an FCPXML in the room or in `~/Desktop/jevid-in`, and opens the FCPXML that lands in `~/Desktop/jevid-out` in Final Cut Pro himself. The CLI below is what the bots run for him. The drop folders keep their `jevid-*` names for now.

The shared object is one timeline plus one brief. The timeline is a Final Cut export, or a starter sequence built from a selects folder. The shared artifact is the JSON report (`protocol` `cut-conductor.room`, `protocol_version` 1).

```text
editor export, or a selects folder via ingest
    → Type & Subs supplies SRT/VTT (optional)
    → Cut Conductor runs named passes (shadow)
    → Pacing / Type & Subs may re-run their own pass
    → human reads the ranked list
    → human accepts ids, or a mechanical auto gate
    → Cut Conductor apply writes a new FCPXML
    → accept/reject events land in taste.json
    → the next decide call sees that taste
```

## Selects folder

The Cut Conductor bot can start from a folder of clips, not only from an export.

```bash
python -m conductor ingest --media selects/ \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --transcript selects.srt \
  --taste taste.json \
  --out-dir out/room
```

`ingest` writes a starter FCPXML, then calls the same `analyze` path. Shadow markers are the default. `--apply` uses the same gate as `apply`: `--accept`, or `--min-confidence` together with `--pass`. Creative passes still do not auto-apply.

The bot posts paths and metadata back into the room, not the picture or the sound:

- the starter path (`ingest.starter_fcpxml`)
- the shadow path (`files.fcpxml`), and `files.applied_fcpxml` only after a gated apply
- the inventory (`ingest.clips`: file name, absolute path, `file://` URL, duration, and `blake2b` of the bytes that were read)
- the brief

Source clips are not modified. `source.fcpxml` and `source.blake2b` refer to the starter XML. The `ingest` object is present only when the run started from a folder. An export-only analyze leaves it out.

Final Cut is still opened by a person. Each `media-rep` `src` is an absolute `file://` URL from the machine that ran ingest. If a path does not resolve, the person uses Relink Files. There is no plugin and no live control.

Order is filename, case-insensitive, and only the folder itself is scanned. Duration comes from `--durations`, otherwise ffprobe, otherwise a 10s placeholder. A placeholder is not the picture's length; the XML carries 10s until a duration is known. The sequence format follows the first clip when ffprobe can read it, and is 1920×1080 at 24fps when it cannot.

`python -m conductor ui` serves a page on 127.0.0.1 that posts a folder path to this command. It is not a webhook and it does not accept media bytes.

## Roles

| bot | owns |
|---|---|
| jevid | the build: orchestrates code work and merges it into this repo |
| Cut Conductor | runs edits: the brief, which passes run, taste, shadow vs apply |
| Pacing | pace preferences and the `pacing` pass |
| Colour | colour |
| Style | the byjustinwu style profile |
| Type & Subs | transcripts, SF Pro subtitles, text treatments, and the `dialogue` pass |

Colour and Style have no pass in the engine yet. The sections below cover the roles the engine already serves.

### Cut Conductor

Owns the brief, which passes run, the taste file, and whether the run is shadow or apply.

- Calls `conductor.analyze(...)` or `python -m conductor analyze|apply`. For a selects folder, calls `conductor.ingest(...)` or `python -m conductor ingest`, which writes a starter FCPXML and then calls `analyze`.
- Actions stay inside `{keep, tighten, remove, mark_review, escalate}`. A bot does not add a sixth.
- Default is shadow. Apply is a separate command and a separate file.
- Applies an unattended cut only when the gate marked it `auto` and the caller passed `--min-confidence` together with `--pass`.
- Applies a review call only when a person named that candidate id with `--accept`.
- Writes `*.conductor.json`. That file is the room state. Do not invent a second schema.

### Type & Subs

Owns the transcript (SRT or WebVTT), subtitles, and text treatments. Times are sequence time, the same clock as the spine, not source-clip time.

- Drives `dialogue` (`--pass dialogue`): a whole filler cue, or a pause of at least 0.80s sitting next to filler.
- Filler is the same whole-cue list cutmcp uses (`um`, `you know`, `i mean`, and their spelling variants). `like`, `yeah`, and `okay` are not filler.
- Dialogue is creative. A confident `tighten` still lands in review. Type & Subs does not auto-apply it.
- When a person keeps a breath or a filler, Type & Subs appends a `reject` (or Cut Conductor does, on the person's behalf). The next dialogue pass sees that event in taste state.

### Pacing

Owns pace preferences: `target_pace`, `jump_cut_tolerance`, `hold_seconds`.

- Drives `pacing` (`--pass pacing`): a long hold, or a clip with very little speech.
- Pacing is creative. Review unless a person accepts the id.
- A `tighten` on a whole clip keeps the first `hold_seconds` (default 4) and lifts the tail. That number comes from taste, not from the model.
- `target_pace: loose` is a hint in Jev state. The mock treats a long silence as a review-level tighten when pace is loose. Live Jev receives the same state; it does not get a rewritten prompt per preference.

### Human

Reads `*.conductor.md`, the HTML page, or the markers after importing the shadow FCPXML into a **duplicate** event.

- `eligible` — high-confidence mechanical calls. Safe for `--min-confidence` on the `mechanical` pass.
- `review` — creative calls, and mechanical calls that missed the auto gate. A person may `--accept` an id whose raw action is `tighten` or `remove`.
- `escalate` — confidence below the review threshold, or Jev said escalate. No unattended cut.
- `keep`, `mark_review`, and `escalate` have no range to lift. `--accept` on those ids errors.

## Passes

A pass is a named slice. Omit `--pass` and the room runs `mechanical`, then `dialogue`, then `pacing`. Repeat `--pass` to run one or a subset, in the order given.

| pass | typical owner | creative | v1 |
|---|---|---|---|
| `mechanical` | Cut Conductor | no | silence gaps, clips under half a second |
| `dialogue` | Type & Subs | yes | transcript filler and the pauses around it |
| `pacing` | Pacing | yes | long holds and low-speech stretches |
| `story` | later | yes | reserved |
| `audio` | later | yes | reserved |
| `broll` | later | yes | reserved |

`story`, `audio`, and `broll` are registered and refused until a generator is attached. The extension point is `conductor.passes.register_pass`. A generator is `(sequences, cues, transcript_present) -> list[Candidate]`. Registering a pass as `creative=True` keeps it out of unattended apply. Registering `creative=False` makes it eligible for the auto gate; do that only for a check a regex or a duration already decided.

Candidate ids (`c0001`, …) are assigned after the passes that actually ran, in timeline order. An id from a dialogue-only report is not the same id in a three-pass report. Bots treat ids as valid for that JSON only.

## Confidence gates

Jev returns a raw action, a confidence, and a risk (the probability that acting would damage the story). `conductor.gates.route` decides the disposition. Thresholds live in taste under `gates`, not in prompt text.

| disposition | default rule | what a bot may do |
|---|---|---|
| `auto` | mechanical, confidence ≥ 0.80, risk ≤ 0.35, raw action is tighten or remove | eligible for `--min-confidence` |
| `review` | confidence ≥ 0.55, or any creative pass, or a mechanical call that missed auto | marker is a to-do. Apply only with `--accept` |
| `escalate` | confidence < 0.55, or raw action is escalate | to-do marker. No unattended cut |
| `keep` | raw action is keep | no marker, no cut |

The marker shows the gated action. The JSON row keeps `raw_action` and `action`. Receipts keep the answers. A bot that wants to explain a review marker reads `raw_action`.

## Accept loop

Shadow (default):

```bash
python -m conductor analyze cut.fcpxml \
  --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --taste taste.json \
  --out-dir out/room
```

The source file is only read. `cut.conductor.fcpxml` adds proposal markers. Edits, keywords, roles, and human markers stay.

Apply, mechanical autos only:

```bash
python -m conductor apply cut.fcpxml \
  --transcript cut.srt \
  --brief "..." \
  --taste taste.json \
  --min-confidence 0.8 \
  --pass mechanical \
  --out-dir out/room
```

Apply, a person signed a review id (dialogue or pacing):

```bash
python -m conductor apply cut.fcpxml \
  --transcript cut.srt \
  --brief "..." \
  --taste taste.json \
  --accept c0003,c0005 \
  --out-dir out/room
```

Rules the bots must not relax:

- `--accept` and the confidence path are alternatives. When ids are present, those ids are what get cut. The confidence path runs only when no ids were named.
- Apply without `--accept`, and without both `--min-confidence` and `--pass`, is an error.
- `--min-confidence` on `dialogue` or `pacing` matches nothing, because those passes are creative. The error tells the caller to `--accept` ids.
- `cut.conductor.applied.fcpxml` is a new file. The export the editor handed over is not modified. A bot checks the source hash in the JSON (`source.blake2b`) against the file on disk if it needs a receipt.
- Import either FCPXML into a duplicate event. There is no plugin and no live timeline write.

A successful apply appends one `accept` event per cut to the taste log and writes `*.taste.json` in the output directory. It does not overwrite the taste file that was passed in.

A reject, or an accept the person wants recorded without cutting yet:

```bash
python -m conductor feedback \
  --taste taste.json \
  --out out/room/taste.json \
  --event reject \
  --id c0004 \
  --action tighten \
  --pass dialogue \
  --note "keep the breath before the explanation"
```

## Taste, and how bots feed it

`taste.json` version 1:

```json
{
  "version": 1,
  "prefs": {
    "jump_cut_tolerance": 0.5,
    "target_pace": "measured",
    "cold_open_bias": "neutral",
    "hold_seconds": 4.0
  },
  "gates": {
    "auto_confidence": 0.8,
    "review_confidence": 0.55,
    "auto_risk_max": 0.35
  },
  "log": [
    {
      "event": "reject",
      "candidate_id": "c0004",
      "action": "tighten",
      "pass": "dialogue",
      "note": "keep the breath before the explanation"
    }
  ]
}
```

`target_pace` is `tight`, `measured`, or `loose`. `cold_open_bias` is `keep`, `neutral`, or `cut`. `jump_cut_tolerance` is 0 to 1. Extra log fields (a room may add `at`) are kept.

When taste is present, Jev state includes:

- `prefs` — the four preferences, unchanged.
- `feedback.accepts` / `feedback.rejects` — counts.
- `feedback.recent` — the last 20 events.

That is the whole learning loop in v1. No model is trained. A later bot can bias its own ranking by reading the same log; it should still pass the file through `--taste` so the decide call and the bot see one object.

Who appends what:

| event | who writes it | when |
|---|---|---|
| `accept` | Cut Conductor, during `apply` | after the new FCPXML is built, one event per cut |
| `accept` | Type & Subs, Pacing, or Cut Conductor via `feedback` | a person agreed and wants it logged before the next pass |
| `reject` | the bot that owns the pass, via `feedback` | a person declined the proposal |

The next analyze/apply that points `--taste` at the updated file puts those events in front of Jev.

## State payload

`*.conductor.json` is what a bot posts back into the room. Fields a bot should rely on:

| field | use |
|---|---|
| `protocol`, `protocol_version` | `cut-conductor.room`, `1` |
| `mode` | `dry-run` or `live` |
| `shadow`, `applied` | shadow is always true for a successful run; `applied` is true only after a cut file was written |
| `brief`, `passes`, `gates`, `taste` | what this run was asked |
| `source.blake2b` | hash of the FCPXML that was read (the export, or the starter from ingest) |
| `ingest` | only when the run started from a folder: starter path, clip paths, durations. Absent on an export-only analyze |
| `changes[]` | ranked rows: `section` is `eligible`, `review`, or `escalate` |
| `changes[].candidate_id` | the id for `--accept` |
| `changes[].raw_action` | what Jev chose, before the gate |
| `changes[].action` | what the marker shows, after the gate |
| `changes[].disposition` | `auto`, `review`, or `escalate` |
| `kept[]` | candidates judged `keep` |
| `cuts[]` | ranges actually removed, present only on apply |
| `receipts[]` | state, questions, and answers for that batch |

`changes` is ordered eligible first (confidence × (1 − risk), highest first), then review, then escalate (least confident first). A bot does not re-sort that list. Calibration of live confidence is unproven; the order is the contract, not a claim that 0.9 means 90%.

## What this room does not do

- No Final Cut plugin, Apple Events, or watch-folder rewrite of the open library.
- No sixth tool on the cutmcp MCP server. `conductor` is a sibling package.
- No story, audio, or b-roll judgments until a generator is registered.
- No training step on the taste log.

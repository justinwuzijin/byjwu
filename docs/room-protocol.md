# Room protocol

How a Grok bot room drives Cut Conductor. The room is the only UX. The editor drops a file and opens the FCPXML the bot hands back. The editor does not run commands.

v1 of this repo is the library and the CLI the Conductor bot runs. No bot process is implemented here, and nothing in this repo talks to Final Cut or to a bot API. This file is the contract those bots call.

The shared object is one timeline plus one brief. The timeline is a Final Cut export, or a starter sequence built from a selects folder the editor dropped. The shared artifact is the JSON report (`protocol` `cut-conductor.room`, `protocol_version` 1). The loop adds `iterate.json` (`protocol` `cut-conductor.iterate`).

```text
editor drops one FCPXML, or a selects folder, into the room
        conventional Mac path: ~/Desktop/jevid-in
        → Conductor bot runs iterate
        → a selects folder is ingested to a starter, then analyzed
        → each round: shadow every pass, auto-apply mechanical gates only
        → bot writes ~/Desktop/jevid-out
        → bot posts the handback path (not the media bytes)
        → editor opens that FCPXML in Final Cut
        → a person answers only on escalate, or when the loop stops at max rounds
        → accept/reject events land in taste.json for the next round
```

## Drop folder

People put work in `~/Desktop/jevid-in`. The Conductor bot writes `~/Desktop/jevid-out`. Any other folder the bot is pointed at writes a sibling named `{folder}-out`, unless the bot passes `--out-dir`.

The drop holds one of these, and only the top of the folder:

- one `.fcpxml` export, or
- video files (`mov`, `mp4`, `m4v`, `mxf`, `avi`, `mkv`, `mts`, `m2ts`)

Optional, in the same folder: `brief.txt`, one `.srt` or `.vtt`, `taste.json`, `durations.json`. Dotfiles and subfolders are ignored. Two FCPXML files, two transcripts, or an export mixed with video files is an error the bot reports back into the room.

```bash
python -m conductor iterate --drop ~/Desktop/jevid-in
```

That is the command the bot runs. It is not a step the editor types.

`iterate` writes `v1/`, `v2/`, … and a summary:

| file | what the bot posts |
|---|---|
| `iterate.json` | `reason`, `handback`, per-round metrics |
| `iterate.md` | the same table, for the room message |
| `vN/…fcpxml` | shadow, and `*.applied.fcpxml` when that round cut |

`reason` is one of `metrics`, `max-rounds`, `no-progress`, `error`.

Stop metrics, with defaults the bot can override:

| metric | default | blocks a stop when |
|---|---|---|
| silence | 1.25s | total gap time on the handback is above it |
| escalations | 0 | open escalate rows are above it |
| duration | off | `--target-seconds` is set and the handback is outside the tolerance (2s) |
| reviews | off | `--max-reviews` is set and open reviews are above it |
| shot length, cuts per minute | reported only | a min or max was set |

A round auto-applies only mechanical calls at or above the taste `auto_confidence` (default 0.80), the same gate as `apply --min-confidence --pass mechanical`. Dialogue and pacing are never cut by the loop. If a round applies nothing and the metrics match the previous round, the bot stops with `no-progress` and asks the room. If the round limit is hit first, the bot stops with `max-rounds` and asks the room. Escalations are already a reason to ask, because the default requires zero of them before the metrics count as clear.

The next round reads the applied FCPXML when the round cut, otherwise the shadow file. The taste file written by that round, including fingerprints of applied cuts, is the taste for the next round. Candidate ids are renumbered every round. The fingerprint (pass, kind, clip name, duration, span) is what carries forward.

The file in `jevid-in` is only read. Source clips are only read.

## Selects folder

When the drop is clips, `iterate` calls `ingest` before the first round. A bot that wants a single shadow pass and no loop calls `ingest` itself:

```bash
python -m conductor ingest --media ~/Desktop/jevid-in \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --transcript ~/Desktop/jevid-in/interview.srt \
  --taste ~/Desktop/jevid-in/taste.json \
  --out-dir ~/Desktop/jevid-out
```

`ingest` writes a starter FCPXML, then calls the same `analyze` path. Shadow markers are the default. `--apply` uses the same gate as `apply`: `--accept`, or `--min-confidence` together with `--pass`. Creative passes still do not auto-apply.

The bot posts paths and metadata back into the room, not the picture or the sound:

- the starter path (`ingest.starter_fcpxml`)
- the shadow path (`files.fcpxml`), and `files.applied_fcpxml` only after a gated apply
- the handback path from `iterate.json` when the bot ran the loop
- the inventory (`ingest.clips`: file name, absolute path, `file://` URL, duration, and `blake2b` of the bytes that were read)
- the brief

`source.fcpxml` and `source.blake2b` refer to the starter XML when the run started from a folder. The `ingest` object is present only on that path. An export-only analyze leaves it out.

Final Cut is still opened by a person, on the file the bot named. Each `media-rep` `src` is an absolute `file://` URL from the machine that ran ingest. If a path does not resolve, the person uses Relink Files. There is no plugin and no live control.

Order is filename, case-insensitive. Duration comes from `durations.json` or `--durations`, otherwise ffprobe, otherwise a 10s placeholder. A placeholder is not the picture's length; the XML carries 10s until a duration is known. The sequence format follows the first clip when ffprobe can read it, and is 1920×1080 at 24fps when it cannot.

`python -m conductor ui` serves a page on 127.0.0.1 that posts a folder path to ingest. It is for the bot and for developers. It is not the editor path, not a webhook, and it does not accept media bytes.

## Roles

### Conductor

Owns the brief, which passes run, the taste file, the drop folder, and the iterate loop.

- Calls `python -m conductor iterate` on the drop. For a single pass, calls `conductor.analyze(...)` or `conductor.ingest(...)`.
- Actions stay inside `{keep, tighten, remove, mark_review, escalate}`. A bot does not add a sixth.
- Default for a single pass is shadow. Apply is a separate command and a separate file.
- The loop applies an unattended cut only when the gate marked it `auto` on the mechanical pass.
- Applies a review call only when a person named that candidate id. The bot then passes `--accept`.
- Writes `*.conductor.json` and, for the loop, `iterate.json`. Those files are the room state. Do not invent a second schema.
- Posts the handback path. Does not post media bytes.

### Transcript

Owns the SRT or WebVTT. Times are sequence time, the same clock as the spine, not source-clip time.

- Drives `dialogue` (`--pass dialogue`): a whole filler cue, or a pause of at least 0.80s sitting next to filler.
- Filler is the same whole-cue list cutmcp uses (`um`, `you know`, `i mean`, and their spelling variants). `like`, `yeah`, and `okay` are not filler.
- Dialogue is creative. A confident `tighten` still lands in review. Transcript does not auto-apply it, and `iterate` does not either.
- When a person keeps a breath or a filler, Transcript appends a `reject` (or the Conductor does, on the person's behalf). The next dialogue pass sees that event in taste state.

### Pacing

Owns pace preferences: `target_pace`, `jump_cut_tolerance`, `hold_seconds`.

- Drives `pacing` (`--pass pacing`): a long hold, or a clip with very little speech.
- Pacing is creative. Review unless a person accepts the id.
- A `tighten` on a whole clip keeps the first `hold_seconds` (default 4) and lifts the tail. That number comes from taste, not from the model.
- `target_pace: loose` is a hint in Jev state. The mock treats a long silence as a review-level tighten when pace is loose. Live Jev receives the same state; it does not get a rewritten prompt per preference.

### Human

Drops the export or the selects folder. Opens the handback in Final Cut. Answers in the room when Conductor escalates, or when iterate stops because it hit `max-rounds` or `no-progress` with work still open.

The ranked list the bot posts:

- `eligible` — high-confidence mechanical calls. The loop may already have cut these.
- `review` — creative calls, and mechanical calls that missed the auto gate. A person may accept an id whose raw action is `tighten` or `remove`. The bot passes that id to `--accept`.
- `escalate` — confidence below the review threshold, or Jev said escalate. The room asks. No unattended cut.
- `keep`, `mark_review`, and `escalate` have no range to lift. `--accept` on those ids errors.

## Passes

A pass is a named slice. Omit `--pass` and the room runs `mechanical`, then `dialogue`, then `pacing`. Repeat `--pass` to run one or a subset, in the order given. `iterate` always runs all three, and only cuts `mechanical`.

| pass | typical owner | creative | v1 |
|---|---|---|---|
| `mechanical` | Conductor | no | silence gaps, clips under half a second |
| `dialogue` | Transcript | yes | transcript filler and the pauses around it |
| `pacing` | Pacing | yes | long holds and low-speech stretches |
| `story` | later | yes | reserved |
| `audio` | later | yes | reserved |
| `broll` | later | yes | reserved |

`story`, `audio`, and `broll` are registered and refused until a generator is attached. The extension point is `conductor.passes.register_pass`. A generator is `(sequences, cues, transcript_present) -> list[Candidate]`. Registering a pass as `creative=True` keeps it out of unattended apply. Registering `creative=False` makes it eligible for the auto gate; do that only for a check a regex or a duration already decided.

Candidate ids (`c0001`, …) are assigned after the passes that actually ran, in timeline order. An id from a dialogue-only report is not the same id in a three-pass report. Bots treat ids as valid for that JSON only. Across iterate rounds, use `fingerprint`.

## Confidence gates

Jev returns a raw action, a confidence, and a risk (the probability that acting would damage the story). `conductor.gates.route` decides the disposition. Thresholds live in taste under `gates`, not in prompt text.

| disposition | default rule | what a bot may do |
|---|---|---|
| `auto` | mechanical, confidence ≥ 0.80, risk ≤ 0.35, raw action is tighten or remove | the iterate loop cuts it; a single apply needs `--min-confidence` |
| `review` | confidence ≥ 0.55, or any creative pass, or a mechanical call that missed auto | marker is a to-do. Apply only after a person accepts the id |
| `escalate` | confidence < 0.55, or raw action is escalate | to-do marker. The room asks a person |
| `keep` | raw action is keep | no marker, no cut |

The marker shows the gated action. The JSON row keeps `raw_action` and `action`. Receipts keep the answers. A bot that wants to explain a review marker reads `raw_action`.

## Accept loop

These commands are what the bot runs after the room has a file.

Shadow (a single pass, no loop):

```bash
python -m conductor analyze cut.fcpxml \
  --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --taste taste.json \
  --out-dir ~/Desktop/jevid-out
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
  --out-dir ~/Desktop/jevid-out
```

Apply, a person in the room signed a review id (dialogue or pacing):

```bash
python -m conductor apply cut.fcpxml \
  --transcript cut.srt \
  --brief "..." \
  --taste taste.json \
  --accept c0003,c0005 \
  --out-dir ~/Desktop/jevid-out
```

Rules the bots must not relax:

- `--accept` and the confidence path are alternatives. When ids are present, those ids are what get cut. The confidence path runs only when no ids were named.
- Apply without `--accept`, and without both `--min-confidence` and `--pass`, is an error.
- `--min-confidence` on `dialogue` or `pacing` matches nothing, because those passes are creative. The error tells the caller to `--accept` ids.
- `cut.conductor.applied.fcpxml` is a new file. The export in the drop folder is not modified. A bot checks the source hash in the JSON (`source.blake2b`) against the file on disk if it needs a receipt.
- The person imports the handback. There is no plugin and no live timeline write.

A successful apply appends one `accept` event per cut to the taste log and writes `*.taste.json` in the output directory. It does not overwrite the taste file that was passed in. The event includes `fingerprint` when the candidate had one.

A reject, or an accept the person wants recorded without cutting yet:

```bash
python -m conductor feedback \
  --taste taste.json \
  --out ~/Desktop/jevid-out/taste.json \
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
  ],
  "carry": {
    "open": [],
    "decisions": []
  }
}
```

`target_pace` is `tight`, `measured`, or `loose`. `cold_open_bias` is `keep`, `neutral`, or `cut`. `jump_cut_tolerance` is 0 to 1. Extra log fields (a room may add `at`, and apply adds `fingerprint`) are kept. `carry` may be omitted; it defaults to empty.

When taste is present, Jev state includes:

- `prefs` — the four preferences, unchanged.
- `feedback.accepts` / `feedback.rejects` — counts.
- `feedback.recent` — the last 20 events.
- `feedback.applied` — fingerprints of accepted cuts. The mock keeps a region whose fingerprint is already in this list, so iterate does not re-litigate it.
- `feedback.open` — fingerprints still in review or escalate at the end of the last round.
- `feedback.decisions` — the last 40 rows `iterate` recorded (round, id, fingerprint, action, whether it was applied).

That is the whole learning loop in v1. No model is trained. A later bot can bias its own ranking by reading the same log; it should still pass the file through `--taste` so the decide call and the bot see one object.

Who appends what:

| event | who writes it | when |
|---|---|---|
| `accept` | Conductor, during `apply` or `iterate` | after the new FCPXML is built, one event per cut, with `fingerprint` |
| `accept` | Transcript, Pacing, or Conductor via `feedback` | a person agreed and wants it logged before the next pass |
| `reject` | the bot that owns the pass, via `feedback` | a person declined the proposal |

The next analyze, apply, or iterate round that points `--taste` at the updated file puts those events in front of Jev. `iterate` does this itself between rounds.

## State payload

`*.conductor.json` is what a bot posts back into the room. Fields a bot should rely on:

| field | use |
|---|---|
| `protocol`, `protocol_version` | `cut-conductor.room`, `1` |
| `mode` | `dry-run` or `live` |
| `shadow` | true when this run did not write a cut file |
| `applied` | true when a cut file was written |
| `brief`, `passes`, `gates`, `taste` | what this run was asked. `taste.feedback.applied` lists fingerprints already cut |
| `source.blake2b` | hash of the FCPXML that was read (the export, or the starter from ingest) |
| `ingest` | only when the run started from a folder: starter path, clip paths, durations. Absent on an export-only analyze |
| `changes[]` | ranked rows: `section` is `eligible`, `review`, or `escalate` |
| `changes[].candidate_id` | the id for `--accept` in this JSON only |
| `changes[].fingerprint` | stable across iterate rounds |
| `changes[].raw_action` | what Jev chose, before the gate |
| `changes[].action` | what the marker shows, after the gate |
| `changes[].disposition` | `auto`, `review`, or `escalate` |
| `kept[]` | candidates judged `keep` |
| `cuts[]` | ranges actually removed, present only on apply |
| `receipts[]` | state, questions, and answers for that batch |

`iterate.json` adds `reason`, `handback`, and `rounds[]` (`applied`, `failures`, `metrics`). The bot posts `handback`. The editor opens that file.

`changes` is ordered eligible first (confidence × (1 − risk), highest first), then review, then escalate (least confident first). A bot does not re-sort that list. Calibration of live confidence is unproven; the order is the contract, not a claim that 0.9 means 90%.

## What this room does not do

- No Final Cut plugin, Apple Events, or live control of the open library.
- No sixth tool on the cutmcp MCP server. Conductor is a sibling package.
- No story, audio, or b-roll judgments until a generator is registered.
- No training step on the taste log.
- No requirement that the editor run a command. The bot runs the CLI after the drop.

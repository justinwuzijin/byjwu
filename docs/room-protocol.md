# Room protocol

How the byjwu Grok Bot room drives the editing engine (the `conductor` package, `python -m conductor`). The repo holds the engine and the CLI. No bot is implemented here, and nothing in this repo talks to Final Cut or to a bot API. This file is the contract those bots call.

The bots coordinate. They do not make editorial decisions. Bounded, logical calls come from Jev (`conductor/jev.py`). Open-ended creative and taste calls come from Claude Opus 5.5 through the Jev/Opus decision router, which is in progress. No Grok model makes an editing decision.

Justin, the owner, does not use the command line. He drops a selects folder, an FCPXML export, a `.fcpxmld` bundle, or a zip in the room or in `~/Desktop/byjwu-in`, and opens the FCPXML that lands in `~/Desktop/byjwu-out` in Final Cut Pro himself. The CLI below is what the bots run for him. The legacy-folder fallback is described under [Desktop folders](#desktop-folders).

The shared object is one timeline plus one brief. The timeline is a Final Cut export, or a starter sequence built from a selects folder. The shared artifact is the JSON report (`protocol` `cut-conductor.room`, `protocol_version` 1).

```text
~/Desktop/byjwu-in  (export, bundle, zip, or a selects folder)
    → room-run detects which
    → Type & Subs supplies SRT/VTT when one is sitting next to the timeline
    → Cut Conductor iterate: analyze, then auto-apply mechanical cuts only
    → ~/Desktop/byjwu-out/<name>-<timestamp>/vN
    → room.md and room.json for the chat
    → stop on metrics, no further mechanical cut, or the round cap
    → a person only for escalate, or when the cap hits
    → accept/reject events land in taste.json
    → the next decide call shifts confidence from that history
```

## Desktop folders

The person does not run the CLI. The bots do.

| folder | who writes it | what it holds |
|---|---|---|
| `~/Desktop/byjwu-in` | the person | a selects folder, one FCPXML export, a `.fcpxmld` bundle, or a zip of either |
| `~/Desktop/byjwu-out/<name>-<timestamp>/` | the Cut Conductor bot | `room.md`, `room.json`, `starter.fcpxml` when the input was a folder, `v1/` … `vN/`, and `iterate.json` |

The bot runs one command. It detects the drop, calls `iterate`, and does not modify the input. A second run writes a new timestamped folder.

```bash
python -m conductor room-run ~/Desktop/byjwu-in/cut.fcpxml \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-root ~/Desktop/byjwu-out
```

A folder of clips, a `.fcpxmld` bundle, or a `.zip` uses the same command. A drop with music goes to the style assembler first when one is installed (`flow` `assemble+iterate`); otherwise it takes the usual path with a warning. The hook contract is in [room-run.md](room-run.md). Dry-run is the default. `--live` calls Jev. `room.md` is what the bot pastes into chat. `room.json` is `protocol` `cut-conductor.room-run`, `protocol_version` 1. It points at `iterate.json` and the per-round `*.conductor.json` files. It does not replace them.

`room-run --watch ~/Desktop/byjwu-in` is the optional inbox process (debounce, skip already processed, log). Operators set that up from [room-run.md](room-run.md). The editor does not run it. Final Cut is still opened by a person, and only to import the FCPXML named in the summary.

With no path, `room-run --watch` watches `~/Desktop/byjwu-in`. `--out-root` defaults to `~/Desktop/byjwu-out`. `iterate` with neither `--fcpxml` nor `--media` reads the same inbox and writes to the same outbox. When `byjwu-in` / `byjwu-out` don't exist but the legacy `jevid-in` / `jevid-out` do, the legacy folders are used and the CLI prints one note line per folder on stderr.

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

## Iterate

`python -m conductor iterate` is the loop the Cut Conductor bot owns. A person is not in the round.

Each round writes `<out-dir>/vN/`:

1. Stage the current timeline as `timeline.fcpxml` (a copy; the dropped file is not modified).
2. Analyze the implemented passes (mechanical, dialogue, pacing, colour), or the subset passed with `--pass`.
3. Auto-apply only mechanical rows the gate marked `auto`, at or above `--min-confidence` (default 0.8). Same rule as `apply --min-confidence --pass mechanical`.
4. If that matched nothing, the round's next file is the shadow FCPXML. If it matched, the next file is the applied FCPXML.
5. Append one taste `accept` per cut, including clip name and kind. The next round loads that taste file. The taste path the bot passed in is not overwritten.

Stop after the round, on the first reason that holds:

| `stop_reason` | when |
|---|---|
| `metrics` | every configured target holds. Unset targets are not constraints. If the timeline is already inside them, the round does not cut. |
| `no-progress` | the round applied no mechanical cut |
| `max-rounds` | the cap (default 5) was hit after a round that did cut |

Configured targets: `--target-seconds` with `--tolerance` (default 1 second, a symmetric window), `--max-escalate`, `--max-review`, `--max-silence-seconds`, `--min-shot-seconds`, `--max-cuts-per-minute`.

Silence is the sum of `silence_gap` candidates (explicit gaps and holes of at least 1.25s). It is not a waveform. Average shot length and cuts per minute are spine arithmetic: a cut is the join between two non-gap clips.

`iterate.json` at the output root is `protocol` `cut-conductor.iterate`, `protocol_version` 1. Bots post `stop_reason`, `needs_human`, `human_reasons`, `applied`, and `rounds`. `decision_usage` is the call counter summed over the rounds. Each row in `rounds` has its own. One router serves every round, so a region that did not change is answered from the cache and costs no call. Each round still has its own `*.conductor.json` (`cut-conductor.room`). `room-run` adds `room.json` (`cut-conductor.room-run`) as the chat summary that points at those files. Do not invent another schema.

`needs_human` is true when the last round's escalate count is above zero, or `stop_reason` is `max-rounds`. A `metrics` or `no-progress` stop with no escalate is the bot finishing. Review rows are marked and left for later. The loop does not `--accept` them.

The transcript stays on the clock of the file you passed. After a ripple, later dialogue times can drift. Mechanical silence and short clips do not use the transcript, so the unattended loop still converges. A bot that cares about filler after a cut should rebuild the SRT on the new sequence clock before the next dialogue pass.

## Roles

| bot | owns |
|---|---|
| byjwu | the build: orchestrates code work and merges it into this repo |
| Cut Conductor | runs edits: the brief, which passes run, taste, shadow vs apply |
| Pacing | pace preferences and the `pacing` pass |
| Colour | colour, and the review-only `colour` pass |
| Style | the byjustinwu style profile |
| Type & Subs | transcripts, SF Pro subtitles, text treatments, and the `dialogue` pass |

Style has no pass in the engine yet. The sections below cover the roles the engine already serves.

### Cut Conductor

Owns the brief, which passes run, the taste file, the iterate loop, and whether a one-shot run is shadow or apply.

- Calls `python -m conductor room-run` for a drop. That calls `iterate` (and, for a clip folder, the starter FCPXML `iterate` already writes). `conductor.analyze` / `conductor.ingest` remain the one-shot library calls.
- Actions stay inside `{keep, tighten, remove, mark_review, escalate}`. A bot does not add a sixth.
- Default for a one-shot command is shadow. Apply is a separate command and a separate file. Iterate's apply is the mechanical auto gate only, and it still writes a new file.
- Applies an unattended cut only when the gate marked it `auto` and the pass is mechanical.
- Applies a review call only when a person named that candidate id with `--accept`. Iterate does not do this.
- Writes `*.conductor.json` per round, `iterate.json` for the loop, and `room.json` / `room.md` for the chat summary. Do not invent another schema.

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

### Colour

Owns picture notes that can be read from the XML, and the honest limit where they cannot.

- Drives `colour`: a spine clip with no role, an asset frame that badly mismatches the sequence (portrait against landscape, or a relative aspect gap of at least 0.15), and one placeholder per sequence for exposure, white balance, and skin.
- Does not decode media. The placeholder says so. A grade of the pixels is not this pass.
- Colour is creative. Jev may answer `mark_review` or `escalate`. The mock escalates an extreme aspect mismatch and marks the role note and the exposure placeholder for review. The gate will not mark colour `auto`, and iterate will not apply it.
- A person `--accept`s a colour id only when the raw action is already `tighten` or `remove`. That ripples a range. It does not change pixels. The mock does not return those actions.

### Human

Opens the FCPXML from `~/Desktop/byjwu-out` when `iterate.json` says `needs_human`. That is an escalate, or a loop that hit `--max-rounds`. Other stops are the bot finishing.

- `eligible` — high-confidence mechanical calls. Iterate already applied these when they cleared the gate. A one-shot `apply` can do the same with `--min-confidence` on the `mechanical` pass.
- `review` — creative calls, including colour, and mechanical calls that missed the auto gate. Left marked. A person may `--accept` an id whose raw action is `tighten` or `remove`. The loop does not wait on these.
- `escalate` — confidence below the review threshold, or Jev said escalate. No unattended cut. This is one of the two reasons the room asks a person.
- `keep`, `mark_review`, and `escalate` have no range to lift. `--accept` on those ids errors.

## Passes

A pass is a named slice. Omit `--pass` and the room runs `mechanical`, then `dialogue`, then `pacing`, then `colour`. Repeat `--pass` to run one or a subset, in the order given. `iterate` uses that set for the judgment and then auto-applies only `mechanical`.

| pass | typical owner | creative | v1 |
|---|---|---|---|
| `mechanical` | Cut Conductor | no | silence gaps, clips under half a second. The only pass `iterate` auto-applies. |
| `dialogue` | Type & Subs | yes | transcript filler and the pauses around it |
| `pacing` | Pacing | yes | long holds and low-speech stretches |
| `colour` | Colour | yes | missing roles, extreme aspect mismatches in the XML, placeholder for exposure and skin. Never an unattended cut. |
| `story` | later | yes | reserved |
| `audio` | later | yes | reserved |
| `broll` | later | yes | reserved |

`story`, `audio`, and `broll` are registered and refused until a generator is attached. The extension point is `conductor.passes.register_pass`. A generator is `(sequences, cues, transcript_present) -> list[Candidate]`. Registering a pass as `creative=True` keeps it out of unattended apply. Registering `creative=False` makes it eligible for the auto gate; do that only for a check a regex or a duration already decided.

Candidate ids (`c0001`, …) are assigned after the passes that actually ran, in timeline order. An id from a dialogue-only report is not the same id in a four-pass report. Bots treat ids as valid for that JSON only.

## Who decides

`conductor.router` sends each candidate to one engine by its decision type. Linear, logical calls go to Jev. Open-ended creative calls go to Claude Opus 5.5. The list and the reason for each entry are `router.DECISION_TYPES`. No Grok or xAI model is in the decision path. Bots in this room run the commands and relay the payload. They do not make the call themselves.

| pass | kinds | engine |
|---|---|---|
| `mechanical` | `silence_gap`, `short_clip` | Jev |
| `dialogue` | `filler_pause` | Jev (still review-only; the pass is creative) |
| `pacing` | `long_static` | Jev (still review-only) |
| `colour` | `colour_role`, `colour_aspect` | Jev (still review-only) |
| `colour` | `colour_unseen` (exposure, skin, look) | Opus |
| `story`, `broll` | anything a generator emits | Opus, unless a type is registered |
| `audio` | anything a generator emits | Jev, unless a type is registered |

When an engine is not there:

- Jev unavailable (no key, HTTP failure, or an unusable answer): the deterministic rules answer, at 0.85× confidence. `engine_source` is `rules`, and the marker note says `engine=jev/rules`. A rules row is never `auto`, whatever the gates or taste priors say, so `iterate` stops with `no-progress` rather than cutting on rules.
- Opus unavailable (no key, HTTP failure, refusal, or an answer that fails the schema): the row is a `review` with raw action `mark_review` and confidence 0. `engine_source` is `unavailable`. Nothing creative is applied.

The first failed request marks that engine down for the rest of the run, including later `iterate` rounds. Each of these cases is a warning in the payload (`decision_usage.warnings`) and on stderr.

The assembly engine asks the same router with `Router.decide([Ask(...)])`. A linear ask gives options and a deterministic `rule`. A creative ask gives options or a JSON schema. The returned `Decision` has `engine`, `source`, `value`, `confidence`, `why`, and `needs_review`.

## Confidence gates

The engine returns a raw action, a confidence, and a risk (the probability that acting would damage the story). `conductor.gates.route` decides the disposition. Thresholds live in taste under `gates`, not in prompt text. The engine and the gate are separate: a confident Jev call on a creative pass is still review.

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
- `--min-confidence` on `dialogue`, `pacing`, or `colour` matches nothing, because those passes are creative. The error tells the caller to `--accept` ids. Colour is still not a pixel grade.
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

The person does not edit `taste.json`. The bot does, from two things the person already produces: a Final Cut re-export, and a sentence in the room.

Nothing is trained. Each kind (`silence_gap`, `short_clip`, `long_static`, `filler_pause`, the colour kinds) gets a prior recomputed from the log. The prior moves that kind's confidence by at most 0.20, and it can only raise the mechanical auto threshold. A call that was under the auto line stays under it unless a rule sets `loosen_auto`. That flag is the opt-in. Creative passes still never take `auto`.

The report row carries `confidence` (after the prior), `confidence_raw` (what Jev or the mock returned), and `taste_reason`. The reason is a sentence like `confidence lowered because you rejected 4/5 similar suggestions`. The same sentence is on the marker note (`taste=`) and under `taste.priors` in the JSON. A bot posts `taste_reason` when it explains a review marker.

### Project file and global profile

`--taste` is the project file. `--global-taste` is optional and read-only during `analyze` and `iterate`. Project prefs, gates, and rules win. Priors read both logs. The taste file a run writes does not copy the global log into the project.

```bash
python -m conductor analyze reexport.fcpxml \
  --brief "..." \
  --taste ~/Desktop/byjwu-out/project.taste.json \
  --global-taste ~/Desktop/byjwu-out/global.taste.json \
  --learn-from ~/Desktop/byjwu-out/v1/timeline.conductor.fcpxml \
  --feedback ~/Desktop/byjwu-in/notes.json \
  --out-dir ~/Desktop/byjwu-out/v2
```

`iterate` takes the same three flags. `--learn-from` and `--feedback` apply on round 1 only. `--global-taste` is read every round. Later rounds keep using the taste file the previous round wrote.

### Diff the re-export

`--learn-from` is the shadow FCPXML the room handed back (the one with Cut Conductor markers). The main file is what the person re-exported after editing in Final Cut. The same pair can be recorded first:

```bash
python -m conductor feedback \
  --taste project.taste.json \
  --out ~/Desktop/byjwu-out/project.taste.json \
  --proposed ~/Desktop/byjwu-out/v1/timeline.conductor.fcpxml \
  --edited ~/Desktop/byjwu-in/reexport.fcpxml
```

Matching uses the media URL and source time, so Final Cut can renumber asset ids. For each Cut Conductor marker:

| what the re-export did | event |
|---|---|
| suggested range is gone | `accept` |
| suggested range is only partly gone | `modify` |
| picture remains, that marker is gone, and some other Cut Conductor marker survived | `reject` |
| picture remains and the marker is still there | nothing (still open) |
| a cut that matches no suggestion | `extra` |

If the re-export has zero Cut Conductor markers, rejects are not inferred. A stripped note is not a reject-all. Accepts, modifies, and extras still are. The bot says so from the warning.

`extra` events name a kind when the cut is recognizable (`silence_gap`, `short_clip`, `long_static`). Anything else is `editor_cut` and does not move the known kinds. Checked-in pair: `fixtures/feedback/proposed.fcpxml` and `fixtures/feedback/edited.fcpxml`.

### Notes from chat

Write one JSON object per thing the person said. `at` is a timecode on the sequence clock. `12:30` is twelve minutes and thirty seconds. No `at`, with a `kind`, is a standing rule.

`fixtures/feedback/notes.json`:

```json
{
  "version": 1,
  "items": [
    {
      "event": "reject",
      "at": "12:30",
      "kind": "long_static",
      "note": "keep the long hold"
    },
    {
      "scope": "rule",
      "event": "accept",
      "kind": "short_clip",
      "action": "remove",
      "when": {"flash": true},
      "note": "always cut flash frames",
      "loosen_auto": true
    }
  ]
}
```

`"keep the long hold at 12:30"` is the first item: `event` `reject`, `kind` `long_static`. It binds to the candidate that contains that time. If this timeline has no such candidate, the note stays in `pending` and the next run tries again.

`"always cut flash frames"` is the second item. `when.flash` matches the short-clip signal. `loosen_auto: true` is the only way a prior may newly qualify a mechanical call for auto-apply. Set it when the person said "always". Leave it off and a positive prior still cannot open the auto gate. A reject rule blocks auto for that kind. Creative passes stay in review either way.

`scope` `global` on an item is written only by `feedback --global-out`. `analyze --feedback` files every item on the project taste.

```bash
python -m conductor feedback \
  --taste project.taste.json \
  --out ~/Desktop/byjwu-out/project.taste.json \
  --global-taste global.taste.json \
  --global-out ~/Desktop/byjwu-out/global.taste.json \
  --notes ~/Desktop/byjwu-in/notes.json \
  --fcpxml ~/Desktop/byjwu-in/reexport.fcpxml
```

`--fcpxml` is how `at` finds a candidate. Without it, timed notes stay pending until the next analyze.

### What the prior does

Counts are per `kind`, from the project log and the global log, deduped by `fingerprint`.

- Rejections lower confidence. Four rejects and one accept of the same kind produce `confidence lowered because you rejected 4/5 similar suggestions`.
- The same rejections raise that kind's auto threshold (at most +0.12 at a total reject rate). Accepts never lower it.
- A positive shift cannot push a below-threshold call across the auto line unless a matching rule has `loosen_auto`.
- Accepts that `iterate` or `apply --min-confidence` made on their own are logged with `source` `auto` and never count. Only a person's `--accept`, a re-export, or a note moves a prior.
- The sentence is stored on the change row. The gate sees the shifted confidence, so a mechanical cut can leave `eligible` and land in `review`. `iterate` then will not auto-apply it.

`taste.json` version 1. `rules` and `pending` are optional.

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
  "rules": [],
  "log": [
    {
      "event": "reject",
      "candidate_id": "c0004",
      "action": "tighten",
      "pass": "dialogue",
      "kind": "filler_pause",
      "note": "keep the breath before the explanation"
    }
  ]
}
```

`target_pace` is `tight`, `measured`, or `loose`. `cold_open_bias` is `keep`, `neutral`, or `cut`. `jump_cut_tolerance` is 0 to 1. Extra log fields are kept. Log events are `accept`, `reject`, `modify`, and `extra`.

When taste is present, Jev state includes `prefs`, `feedback` counts (`accepts`, `rejects`, `modifies`, `extras`, and the last 20 events), `priors`, and `rules`. The confidence shift is applied after the answer, in the gate, not by asking the model to do arithmetic.

Who appends what:

| event | who writes it | when |
|---|---|---|
| `accept` | Cut Conductor, during `apply` or `iterate` | after the new FCPXML is built, one event per cut |
| `accept`, `reject`, `modify`, `extra` | Cut Conductor, from `--learn-from` or `feedback --proposed` | the re-export differs from the shadow file |
| `accept` / `reject` | the bot, via `feedback --event` or a notes file | the person said so in the room |
| rule | the bot, via a notes item with a `kind` and no `at` | a standing preference, including `loosen_auto` |

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
| `changes[].confidence` | after the taste prior; this is what the gate used |
| `changes[].confidence_raw` | Jev or the mock, before the prior |
| `changes[].taste_reason` | why the prior moved, or null |
| `learned[]` | events folded in from `--learn-from` or `--feedback` this run. A standing rule has `record` `rule` and is not a log row |
| `kept[]` | candidates judged `keep` |
| `changes[].engine` | `jev` or `opus`: the engine the call was routed to |
| `changes[].engine_source` | `live`, `mock` (dry-run), `rules` (Jev fallback), or `unavailable` (Opus fallback) |
| `changes[].decision_type`, `changes[].engine_why` | the classification entry and its reason |
| `changes[].engine_model`, `changes[].rationale`, `changes[].cached` | the model that answered, Opus's one-line note, and whether the answer came from the run cache |
| `cuts[]` | ranges actually removed, present only on apply. Each names the `engine` that made the call |
| `decision_usage` | the call counter: per engine `calls`, `live_calls`, `mock_calls`, `failed_calls`, `items`, `cache_hits`, `fallback_items`, `unavailable_items`, tokens, `provider_cost_usd`, `estimated_cost_usd`, `status`, `down_reason`. `totals` sums them |
| `routing` | the decision types this run used, with engine and reason |
| `receipts[]` | per batch: `engine`, `source`, the state, the questions (or the schema), and the answers |

`changes` is ordered eligible first (confidence × (1 − risk), highest first), then review, then escalate (least confident first). A bot does not re-sort that list. Calibration of live confidence is unproven; the order is the contract, not a claim that 0.9 means 90%.

## What this room does not do

- No Final Cut plugin, Apple Events, or watch-folder rewrite of the open library.
- No sixth tool on the cutmcp MCP server. `conductor` is a sibling package.
- No story, audio, or b-roll judgments until a generator is registered. Colour is registered and review-only; it does not decode the picture.
- No unattended cut outside the mechanical auto gate. Iterate does not accept review ids on its own.
- No Desktop watcher in this repo. `~/Desktop/byjwu-in` and `~/Desktop/byjwu-out` are the folders the bot is told to use.
- No trained model on the taste log. Priors are a bounded count, recomputed from the log, and they cannot loosen mechanical auto-apply without `loosen_auto`.

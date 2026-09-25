# jevid

A Grok Bot room that cuts with you. You drop a path. The room runs Cut Conductor until the mechanical cuts are done. You open the FCPXML in Final Cut when a person is actually needed.

Not an auto-editor. Not remote control of the Final Cut window. The handoff is FCPXML. Every Jev call has a confidence and a receipt.

## The path you use

You do not run the CLI. The bots do.

1. Put a selects folder, a Final Cut **File → Export XML…** file, a `.fcpxmld` bundle, or a zip of either, in `~/Desktop/jevid-in`.
2. The room runs `python -m conductor room-run` on that drop. That is the one command the bots use.
3. Results land in a new folder under `~/Desktop/jevid-out`, named for the drop and the time. Inside it: `v1/`, `v2/`, … and a short summary (`room.md`). The path in that summary is the file to open in Final Cut.
4. You step in when a row is an escalate, or the loop hits its round cap. Review markers stay on the timeline. The loop does not apply them.

`~/Desktop/jevid-in` and `~/Desktop/jevid-out` are ordinary folders on the machine the bot runs on. jevid does not upload picture or sound, and it does not drive Final Cut. You import the file the summary names (**File → Import → XML…**). Import creates a new event. It does not patch the project you already have open.

A folder of clips becomes a starter sequence first (filename order, absolute `file://` paths), then the same loop. An export is iterated as it stands. Source clips and the file you dropped are only read.

## Who it’s for

Final Cut editors who want a decision layer and a small crew around a cut: someone on pace, someone on the transcript, someone on colour, and someone who will not change the timeline unless the call is solid or a person said yes.

## How it fits together

| Piece | What it does |
|---|---|
| **Final Cut Pro** | The timeline is the truth. You import the FCPXML jevid writes. You export XML when you already have a cut. |
| **Cut Conductor** | The program in this repo. Named passes, confidence gates, proposal markers, an explicit apply, and the iterate loop the room owns. |
| **Jev** (TypeSafe) | Every linear, logical call: is this gap removable, is this a flash frame, keep or cut a take under rules, does a cut meet the gate. Typed decisions only: keep, tighten, remove, mark for review, or escalate. It does not write the marker text. |
| **Claude Opus 5.5** (Anthropic) | Every open-ended creative call: story shape, which moments carry the video, music feel, type and visual treatment, montage. Answers are JSON checked against a schema. Nothing it says is cut without a gate or a person. |
| **Grok Bot room** | Conductor, Pacing, Transcript, and Colour. They run the loop, explain the list, and ask a person only for an escalate or when the round cap hits. The contract is the [room protocol](docs/room-protocol.md). |

Jev picks from options the code defines. The note on a marker is assembled afterwards from the action, the confidence, and the reason.

## Who decides what

`conductor/router.py` holds the list. Each decision type names its engine and the reason. Every row in the report, and every marker note, says which engine made the call (`engine=jev/live`, `engine=opus/mock`, …).

| Engine | Decision types | When the engine is not there |
|---|---|---|
| Jev | `silence_gap`, `short_clip`, `filler_pause`, `long_static`, `colour_role`, `colour_aspect`, `take_keep`, `take_compare`, `cut_gate`, `pacing_violation`, `subtitle_break`, `audio_check` | The deterministic rules answer instead, at 0.85× confidence (`engine_source` `rules`). Never another model. A rules call is never `auto`. |
| Opus | `colour_unseen`, `story_structure`, `key_moments`, `music`, `typography`, `visual_treatment`, `montage`, `broll_selection` | The call becomes a review marker with no action (`engine_source` `unavailable`). Nothing is auto-applied. |

The engine decides. The gate still decides who may act: dialogue filler is a Jev call, and it stays in review because the pass is creative. Threshold comparisons inside the gate are arithmetic, so they stay code.

No Grok or xAI model is in the decision path. The room bots run commands and post reports. They do not make the calls. A Grok or xAI model id or URL in `CONDUCTOR_JEV_MODEL`, `CONDUCTOR_OPUS_MODEL`, or the host overrides is refused.

Calls are batched: one Jev request per 24 candidates (48 questions), one Opus request per 12. Answers are cached by content, not by id or position. So `iterate` round 2 only asks about regions that changed, and identical regions are asked once. The first failed request marks that engine down for the rest of the run. Every report has a `decision_usage` counter: calls per engine (live and mock), items, cache hits, fallbacks, and tokens and cost when the host returns them. The CLI prints it as a `decisions` line.

The assembly engine and future passes call the same router: `Router.decide([Ask(...)])`. See the docstring in `conductor/router.py`. A new decision type is a `register_decision(name, engine=..., question=..., why=...)`.

## The loop

```text
~/Desktop/jevid-in  (a folder, an export, a bundle, or a zip)
        →  room-run detects which
        →  starter sequence when the input is a folder of clips
        →  iterate: analyze, then auto-apply only mechanical cuts the gate allows
        →  ~/Desktop/jevid-out/<name>-<time>/vN  and room.md
        →  stop when the metrics hold, when nothing mechanical is left, or at the round cap
        →  a person, only for escalate or max rounds
        →  you open the FCPXML named in the summary
        →  a re-export or a note in the room updates taste, and the next gate moves
```

On the checked-in interview, round 1 lifts the long silence. The “um”, the hold, and the colour placeholder stay marked and are not cut. Round 2 finds no further mechanical cut and stops. The export you made in Final Cut is still sitting there, unchanged. No plugin was attached.

## Commands the room runs

From the repo root. No API key. Bots shell out to one command. The desktop folders above are the paths it passes.

```bash
pip install -e ".[dev]"
```

```bash
python -m conductor room-run ~/Desktop/jevid-in/cut.fcpxml \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-root ~/Desktop/jevid-out
```

The same command takes a `.fcpxml`, a `.fcpxmld` bundle, a `.zip` of either, or a folder of clips. It detects which, and it does not modify the drop. Dry-run is the default. `--live` is how a bot calls Jev. An SRT or WebVTT sitting next to the timeline is picked up; `--transcript` overrides that. A `durations.json` in a clip folder is picked up the same way.

When the drop also carries music (`.mp3`, `.wav`, `.aif`, `.m4a`, and similar), room-run hands it to a style assembler first (`--style`, default `byjustinwu`) if one is installed, then runs the loop on what it built. Without one, the clips are handled as above and the summary says the music was not placed.

Each run writes a new folder, `~/Desktop/jevid-out/<name>-<timestamp>/`, so repeating it is safe. `room.md` in that folder is the chat summary (input kind, duration before and after, cuts with timecodes, rows flagged for the editor, stop reason, which signals were available, and the file to open). `room.json` is the same summary. The shadow FCPXML is always there.

`python -m conductor room-run --watch ~/Desktop/jevid-in` processes new drops after the copy has finished, and skips ones it has already recorded. Setup for that process is in [docs/room-run.md](docs/room-run.md). That note is for the person who runs the bot, not for the editor.

What `room-run` calls is the iterate loop:

```bash
python -m conductor iterate \
  --media ~/Desktop/jevid-in \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --transcript ~/Desktop/jevid-in/interview.srt \
  --out-dir ~/Desktop/jevid-out \
  --max-rounds 5
```

An export instead of a folder uses `--fcpxml` and omits `--media`. One of those two inputs, not both. `room-run` chooses.

Each round writes `vN/` inside the output folder: a shadow FCPXML, and an applied FCPXML only when a mechanical auto-gate cut landed. The next round reads the applied file, or the shadow when nothing was cut. `iterate.json` in that folder is the stop record: the per-round metrics, the cuts, and `stop_reason` (`metrics`, `no-progress`, or `max-rounds`).

Stop when every metric you set is true, when a round applies nothing, or at `--max-rounds` (default 5). Metrics are optional. Leave them unset and the loop runs until the mechanical cuts run out or the cap hits.

| flag | stops when |
|---|---|
| `--target-seconds` + `--tolerance` (default 1s) | duration is inside that window |
| `--max-escalate` | escalate rows are at or under the cap |
| `--max-review` | review rows are at or under the cap |
| `--max-silence-seconds` | silence-gap candidates sum to at most this |
| `--min-shot-seconds` | average non-gap spine clip is at least this long |
| `--max-cuts-per-minute` | joins between spine shots, per minute, are at or under this |

`silence_seconds` in the stop record is the sum of gap and hole candidates of at least 1.25s. Shot length and cuts per minute are read off the spine. If the timeline is already inside the metrics, the round does not cut.

When the bot machine can read a referenced media file, quiet stretches inside a clip are also mechanical candidates, and a local transcript can feed dialogue and pacing. If the file or the tool is missing, that stage is skipped and the XML-only run still finishes. What the bot should install, and the `signals` fields to quote, are in the [room protocol](docs/room-protocol.md#media-signals).

Auto-apply uses the same gate as `apply --min-confidence 0.8 --pass mechanical`. Dialogue, pacing, and colour are judged and marked. They are not cut. Taste from `--taste` is carried forward; each round's accepts are appended and the next round sees them. The taste file you passed in is not overwritten.

A person is asked when the last round still has an escalate, or the stop reason is `max-rounds`. A `metrics` or `no-progress` stop with an empty escalate list is the bot finishing.

The checked-in fixture, no API key:

```bash
python scripts/iterate_dry_run.py
```

### From a folder of clips

The checked-in fixture is three placeholder files, not playable media. Durations are in `fixtures/selects/durations.json`, so the dry-run does not need ffprobe or a camera card:

```bash
python scripts/ingest_dry_run.py
```

That writes `out/selects-dry-run/`. The same command, spelled out:

```bash
python -m conductor ingest \
  --media fixtures/selects \
  --durations fixtures/selects/durations.json \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-dir out/selects-dry-run
```

On a real folder:

```bash
python -m conductor ingest \
  --media ~/Selects \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --transcript ~/Selects/interview.srt \
  --out-dir out/selects
```

`--brief`, `--transcript`, and `--taste` are optional. With no brief, the run uses a filename-order assembly line. The transcript is SRT or WebVTT on the sequence clock.

What you get:

| file | what it is |
|---|---|
| `Selects.fcpxml` | starter sequence. Filename order, one spine, no transitions. |
| `Selects.conductor.fcpxml` | the same sequence plus proposal markers. |
| `Selects.conductor.applied.fcpxml` | only with `--apply`, and only for cuts the gate allows. |
| `Selects.conductor.md` / `.json` | the ranked list. The JSON includes an `ingest` object: paths, durations, and a hash of each clip. |

`Selects` is the folder name. `--sequence` replaces it.

Import the starter or the shadow file with **File → Import → XML…**. Import creates a new event. It does not patch a project you already have open, and it does not change the clip files.

**Relink.** Each `media-rep` `src` is an absolute `file://` URL, the path on the machine that ran ingest. Final Cut can open the media when that path resolves. If you generated the XML somewhere else, or the volume is not mounted, use **File → Relink Files…**. jevid does not copy media into a library.

**Durations.** `--durations` wins when it names the file (`8`, `8s`, or `1/8s`). Otherwise Conductor runs ffprobe. If ffprobe is missing or cannot read the file, that clip is **10 seconds** in the XML. That placeholder is not the picture's length. Final Cut will use the 10s written in the XML until you set a real duration and run again. Install ffmpeg so ffprobe is on `PATH` before you ingest a real folder.

**What the folder scan does.** This folder only, not subfolders. Dotfiles are skipped. Containers: `mov`, `mp4`, `m4v`, `mxf`, `avi`, `mkv`, `mts`, `m2ts`. Order is the file name, case-insensitive. The brief does not reorder clips. When ffprobe can read a file, that file's frame size and rate are written on its asset; the sequence format follows the first clip. Unprobed clips are 1920×1080, 24fps.

Shadow markers are the default. To also write the applied file, opt in. The gates are the same as `apply`:

```bash
python -m conductor ingest --media ~/Selects --brief "..." \
  --apply --min-confidence 0.8 --pass mechanical --out-dir out/selects
```

`--accept` and `--min-confidence` do nothing unless you also pass `--apply`. Creative passes are not cut by the confidence switch.

A local page, still on disk paths:

```bash
python -m conductor ui
```

Open `http://127.0.0.1:8765`. Type the folder path, or drop a `file://` path. **Choose folder…** asks the OS for a directory when a display is available. The form does not upload video.

### From an existing cut

```bash
python scripts/dry_run.py
```

That runs the checked-in interview and writes `out/sample-dry-run/`. On your own export:

```bash
python -m conductor analyze cut.fcpxml \
  --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-dir out/cut
```

Read `cut.conductor.md`. It opens with editor's notes (pace, bare gaps, holds, reprises, picture) and then the ranked tables. The companion `cut.conductor.fcpxml` is the same edit with proposal markers added. Import it into a **duplicate event** if you want those markers in Final Cut. Importing creates a new project. It does not patch the one you exported.

The transcript is optional SRT or WebVTT. Times are the sequence clock, not the source clip.

To call live engines, copy `.env.example` and pass `--live`. Dry-run stays the default even when a key is present. `CONDUCTOR_DRY_RUN=1` forces both mocks anyway.

| Variable | Engine | Host | Model |
|---|---|---|---|
| `OPENROUTER_API_KEY` | Jev | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` |
| `TYPESAFE_API_KEY` | Jev | `POST https://api.typesafe.ai/v1/systemone` | `jev-1.13.0` |
| `ANTHROPIC_API_KEY` | Opus | `POST https://api.anthropic.com/v1/messages` | `claude-opus-5-5` (`CONDUCTOR_OPUS_MODEL`) |

OpenRouter wins when both Jev keys are set. `CONDUCTOR_JEV_PROVIDER=typesafe` forces the other. The TypeSafe host rejects the slug `jev-1.13`, so the client sends `jev-1.13.0` there.

`--live` needs at least one engine key. With only a Jev key, creative calls become review markers. With only an Anthropic key, linear calls use the rules. Both cases are warnings on stderr and in the report. Opus requests use `output_config.format` (JSON schema) and `output_config.effort` (`CONDUCTOR_OPUS_EFFORT`, default `medium`). Opus 5.5 rejects forced tool use and disabled thinking, so neither is sent.

`python -m conductor` is the command to use from the repo. A `cut-conductor` script is installed with the package and may land outside your `PATH`.

## Passes, gates, apply, taste

Passes run in order — `mechanical`, then `dialogue`, then `pacing`, then `colour` — or one at a time with `--pass`. `iterate` judges that same set and auto-applies only `mechanical`.

| Pass | Looks for | Who may apply it |
|---|---|---|
| `mechanical` | Silence of at least 1.25s: a bare gap, the uncovered stretch of a gap that has connected clips on it, a hole, or quiet audio inside a clip when the file can be read. A stretch under a connected clip is not silence. Clips under 0.45s (under 0.20s is a flash). | High-confidence tighten/remove, with `--min-confidence`. This is what `iterate` auto-applies. |
| `dialogue` | A whole filler cue (0.25–3s), or a pause of at least 0.80s beside filler | Review, unless you `--accept` the id |
| `pacing` | A long hold. With a transcript: 20s under 0.40 words/second. Without one: a hold/slate/b-roll name, a shot at least 4× the shots around it, or 45s when the timeline is too short to compare. Also review notes for a gap sitting under connected clips, a repeated source range, a sudden rhythm change, a mixed frame rate, an untrimmed string-out, a silent generator card, and a music bed that ends early. | Review, unless you `--accept` a hold. The notes are not lifts. |
| `colour` | A spine clip with no role. An asset frame that badly mismatches the sequence (portrait against landscape, or about 15% off). A placeholder for exposure and skin. | Review or escalate. The picture is not decoded. Never an unattended cut, and never a grade of the pixels. |
| `story`, `audio`, `broll` | Not built | `conductor.passes.register_pass` |

Filler matches the whole cue (`um`, `you know`, `i mean`, and the same list cutmcp uses). `like`, `yeah`, and `okay` are not filler.

Jev returns a raw action, a confidence, and a risk. The gate decides what happens next. Defaults:

| | Rule | Result |
|---|---|---|
| Auto | Confidence ≥ 0.80, risk ≤ 0.35, and the pass is mechanical | Eligible for `--min-confidence` |
| Review | Confidence ≥ 0.55, or any creative pass | To-do marker. `--accept` can still cut a raw tighten or remove |
| Escalate | Confidence below 0.55, or Jev said escalate | To-do marker. No unattended cut |

Creative passes never take the auto disposition, no matter how sure the model is. Thresholds can be overridden in a taste file under `gates`. The numbers in a dry-run come from a local mock so you can read a fixture without a key. They are not evidence that live Jev is calibrated. Treat live confidence as unproven until you have checked it on your own marks.

Apply writes a **second** FCPXML. It does nothing unless you name ids or set a confidence floor and a pass. `ingest --apply` uses this same rule.

```bash
python -m conductor apply cut.fcpxml --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --min-confidence 0.8 --pass mechanical --out-dir out/cut

python -m conductor apply cut.fcpxml --transcript cut.srt \
  --brief "..." --accept c0003 --out-dir out/cut
```

`cut.conductor.applied.fcpxml` is the cut. `cut.fcpxml` is untouched. `--min-confidence` on dialogue, pacing, or colour matches nothing, because those passes are creative. Name the id. Colour still does not grade pixels: there is no picture decode, and an accept only ripples a range when the raw action is already a tighten or remove. The colour mock does not return those actions.

A `tighten` on a whole clip keeps the first `hold_seconds` (default 4) and lifts the tail. A `remove`, a filler, or a hole lifts that range and closes the gap. A transition on the spine is refused rather than left at a stale offset.

Taste is a JSON file: `jump_cut_tolerance`, `target_pace` (`tight`, `measured`, `loose`), `cold_open_bias` (`keep`, `neutral`, `cut`), `hold_seconds`, plus a feedback log. Pass it with `--taste`. An optional `--global-taste` is read-only and does not replace the project file.

The log is not a training set. It becomes a per-kind prior: rejections lower confidence and can only make the mechanical auto gate stricter. Accepts may raise confidence, and they cannot newly open auto-apply unless a standing rule sets `loosen_auto`. The report says why, in a sentence on the row (`taste_reason`). A re-export diff and a notes file are how that log gets written. The room contract is in [docs/room-protocol.md](docs/room-protocol.md). See `fixtures/taste.json` and `fixtures/feedback/`.

```bash
python -m conductor feedback \
  --taste fixtures/taste.json \
  --out out/taste.json \
  --event reject --id c0004 --action tighten --pass dialogue \
  --note "keep the breath"
```

Each run can also write `*.conductor.json` (the room payload), `*.conductor.html` (`--html`), and `*.taste.json`. Marker color is the `color=` field of the note, because FCPXML markers have no color attribute. Review and escalate markers are to-dos (`completed="0"`). A marker’s `start` is in the clip’s source time.

Roles, the accept loop, and the payload fields are in [docs/room-protocol.md](docs/room-protocol.md).

## Safety

- The default command is `analyze`. `ingest` without `--apply` does not cut. `iterate` cuts only mechanical auto-gate rows, into a new file under `vN/`.
- Colour is creative. `--min-confidence --pass colour` matches nothing. `iterate` does not put colour on the apply path.
- `apply`, and `ingest --apply`, error unless you pass `--accept`, or both `--min-confidence` and `--pass`.
- The confidence path only cuts `auto` rows. Creative calls stay in review or escalate.
- Output paths that resolve to the source FCPXML, or to a source clip, are refused. The source bytes are checked at the end of the run.
- Ingest hashes each clip before and after. A change aborts the run.
- There is no Final Cut plugin and no live control of the open library.
- The local page binds to 127.0.0.1 and rejects multipart uploads.

## The Grok Bot room

Conductor, Pacing, Transcript, and Colour are how a cut moves. A person drops a path in `~/Desktop/jevid-in` and opens whatever lands in `~/Desktop/jevid-out` when the room asks. v1 of the software is the library and the CLI those bots call. The contract is [docs/room-protocol.md](docs/room-protocol.md): who owns which pass, how `iterate` stops, and how a re-export or a chat note becomes a prior on the next gate.

The Conductor bot runs `python -m conductor room-run`. That command detects the drop and calls `iterate` (a clip folder is ingested as the starter sequence, then iterated). It does not invent a sixth action. It does not apply a cut the gate did not allow unless a person accepted that id. Unattended cuts are mechanical only. Paste `room.md` into the room. The per-round JSON report (`protocol` `cut-conductor.room`) is still the state behind each round. `iterate.json` (`protocol` `cut-conductor.iterate`) is the stop record. `room.json` (`protocol` `cut-conductor.room-run`) is the chat summary. Bots do not re-sort those lists.

When the input is a folder, the room is woken with the starter FCPXML path, the inventory, the brief, and the media paths. Not with the media bytes.

The bots are orchestration, not a decision engine. A call about the cut goes through the router to Jev or Opus, and the bot posts what came back.

## Roadmap

- **Ordering.** Ingest is filename order. A brief does not reorder clips yet.
- **Taste.** Per-kind priors shift later confidence from rejections, accepts, and editor re-exports. They do not train a model, and they do not loosen mechanical auto-apply unless a rule opts in.
- **More passes.** `colour` is a review-only scaffold: roles and aspect from the XML, and an honest placeholder where exposure and skin would need the picture. `story`, `audio`, and `broll` are still reserved. A new check is a `register_pass`, not a new product.
- **A Mac drop helper.** `room-run --watch` plus the launchd example in [docs/room-run.md](docs/room-run.md) is how an operator keeps the inbox running. The editor still does not run a command. Still FCPXML out, still no plugin.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
python scripts/ingest_dry_run.py
python scripts/iterate_dry_run.py
```

No API key. Conductor tests cover the parser, marker write-back, the mock client, the gates, apply, ingest (including the fixture folder and the local page), the colour pass, iterate (two rounds, the round cap, and a duration window), and `room-run` (an FCPXML, a `.fcpxmld` bundle, a zip, a clip folder, bad drops, and the watcher). The placeholder clips under `fixtures/selects/` are a few bytes each.

`tests/test_conductor_router.py` covers the decision router against fake Jev and Anthropic hosts (`httpx.MockTransport`). It checks the classification, attribution on rows and markers, the rules fallback and the review fallback, bad or refused Opus answers, batching, the cache across runs and iterate rounds, the call counter, and that no key reaches a report.

## Also in this repo: cutmcp

cutmcp is a separate MCP server: raw interview footage in, an EDL out, five tools (`ingest`, `estimate`, `cut`, `review`, `export_timeline`). It is not the Final Cut co-pilot and it is not a sixth pass. Install and tool docs: [docs/cutmcp.md](docs/cutmcp.md).

The Python project name on disk is still `cutmcp`, so an editable install keeps working. For Final Cut, run `python -m conductor`.

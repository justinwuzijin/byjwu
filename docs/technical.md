# byjwu: technical docs

Everything about how byjwu works and how to run it. The short version is in the [README](../README.md).

byjwu helps Justin ([@byjustinwu](https://www.youtube.com/@byjustinwu) on YouTube) edit his YouTube videos using TypeSafe Jev and Claude Opus 5.5. A full Grok Bot orchestration, a room of specialist bots, works out everything stylistic and taste-related about his editing: typography, pacing, style, colours, subtitles, digital assets (the abstract rectangle background layer), and music fades and ducking.

The goal is raw footage and music in, and a finished FCPXML out that imports into Final Cut Pro and feels like a byjustinwu video. The style is learned from his published YouTube videos and gets better each time he re-exports a corrected cut.

Every Jev call has a confidence and a receipt. Nothing here is remote control of the Final Cut window. The handoff is FCPXML.

The engine module is still called `conductor` and still runs as `python -m conductor`, and the `cutmcp` / `cut-conductor` console scripts are unchanged. Those names stay so work already in flight keeps merging. Cut Conductor is now the name of the room bot that runs edits. Links to the GitHub repo still use `github.com/justinwuzijin/jevid` until Justin renames it.

## How Justin uses it

Justin does not use the command line. The room bots do.

1. He drops a selects folder, a Final Cut **File → Export XML…** file, a `.fcpxmld` bundle, or a zip of either, in the Grok Bot room or in `~/Desktop/byjwu-in`.
2. The Cut Conductor bot runs `python -m conductor room-run` on that drop. That is the one command the bots use.
3. Results land in a new folder under `~/Desktop/byjwu-out`, named for the drop and the time. Inside it: `v1/`, `v2/`, … and a short summary (`room.md`). The path in that summary is the file to open in Final Cut.
4. He steps in when a row is an escalate, or the loop hits its round cap. Review markers stay on the timeline. The loop does not apply them.
5. He opens the FCPXML the summary names in Final Cut Pro himself (**File → Import → XML…**). Import creates a new event. It does not patch the project he already has open.

FCPXML goes in and FCPXML comes out. `~/Desktop/byjwu-in` and `~/Desktop/byjwu-out` are ordinary folders on the machine the bot runs on. byjwu does not upload picture or sound, and it does not drive Final Cut. If `byjwu-in` / `byjwu-out` don't exist but the older `jevid-in` / `jevid-out` do, the engine uses the legacy folders and prints a one-line note.

A folder of clips becomes a starter sequence first (filename order, absolute `file://` paths), then the same loop. An export is iterated as it stands. Source clips and the file he dropped are only read.

## What changes in the FCPXML

The short version is in the [README](../README.md). This is the same behaviour, with the file names the code writes.

The run folder is `~/Desktop/byjwu-out/<name>-<YYYYMMDD-HHMMSS>/`. A clip folder also writes `starter.fcpxml` there. Each round is `vN/`:

- `timeline.fcpxml` is a byte copy of the timeline that round reads.
- `timeline.conductor.fcpxml` is the marked copy. `apply_markers` adds `<marker>` elements and refuses the write if a clip's offset, start, duration, ref, or roles change, or if an existing marker's start, value, note, or completed flag changes. The file is still re-serialized, so whitespace and the XML declaration can differ from the export.
- `timeline.conductor.applied.fcpxml` is written only when at least one deletion is applied. It is a fresh parse of that round's timeline, then the deletions, with no proposal markers added.
- `timeline.conductor.md`, `timeline.conductor.json`, and `timeline.taste.json` are the round report. `room.md` and `room.json` are the chat summary. `iterate.json` is the stop record. `timeline.words.json` appears when word timings were read.

Marker `start` is the clip's source time (the same clock as the clip's `start`), at the candidate's timeline position, clamped inside the clip and nudged one frame if that time is already used. `duration` is one sequence frame. The value is `CC {tighten|remove|review|escalate} · {label} @ {timecode}`. The note begins `Cut Conductor shadow proposal. No edit was applied.` Review and escalate set `completed="0"` (a to-do). An `auto` disposition omits `completed` (a standard marker). `keep` writes nothing. FCPXML has no marker colour attribute; `color=` is text inside the note.

`iterate` auto-applies only the mechanical pass, at `--min-confidence` 0.80, and only rows whose disposition is `auto` (confidence at least 0.80, risk at most 0.35, and not a rules fallback). A `remove` lifts the candidate range. A `tighten` on a whole clip lifts everything after `hold_seconds` (default 4). The ripple rewrites spine `offset` values after the cut, and `start` / `duration` (and `audioStart` / `audioDuration` when those attributes exist and the clip has no time map) on a split piece. Sequence `duration` shrinks by the removed time. Connected items and secondary storylines keep their offsets. Coverage under a laned item is punched out of a wholesale removal. A connected item that crosses a cut is dropped and named in the warnings. A non-clip spine item (a transition) aborts the ripple. Audio volume keyframes are not authored. Colour does not grade pixels.

Graphics (`conductor/graphics`) stay off unless `--graphics` is set or the style profile sets `graphics.enabled` (the shipped `byjustinwu` profile leaves it false). The stage then writes onto the file the summary names, on a free lane, and does not change existing clip timing. Subtitles and `none` / `scale_warp` titles are Basic Title elements. Other title treatments are alpha movies in `<stem>.assets/`. Rectangles are Shapes generators with transform keyframes, Gaussian blur, and Hue/Saturation. A second pass leaves a timeline that already has a `byjwu ` asset.

A logic-first decision mode is not on this branch. Measured rules such as uncovered black and dead air cutting by default, with models only able to veto, are still in progress.

## Who decides what

| Piece | Decides | Does not |
|---|---|---|
| **Jev** (TypeSafe) | Every linear, logical call: is this gap removable, is this a flash frame, keep or cut a take under rules, does a cut meet the gate. Typed decisions only: keep, tighten, remove, mark for review, or escalate, each with a confidence and a risk. It picks from options the code defines (`conductor/jev.py`). | Write text, or make open-ended taste calls. |
| **Claude Opus 5.5** (Anthropic) | Every open-ended creative and taste call: story shape, which moments carry the video, music feel, type and visual treatment, montage, and graphics built through code (text treatments, the rectangle background layer). Answers are JSON checked against a schema. Nothing it says is cut without a gate or a person (`conductor/opus.py`). | Generate video. Opus does not generate video natively. |
| **Grok Bot room** | Coordination: routes work, runs the engine, posts paths and reports, and asks Justin when a call needs him. | Editorial work. No Grok model makes an editing decision. |
| **Final Cut Pro** | The timeline is the truth. Justin imports the FCPXML byjwu writes, and exports XML when he already has a cut. | — |

The note on a marker is assembled afterwards from the action, the confidence, and the reason. No model writes it.

### The decision router

`conductor/router.py` holds the list. Each decision type names its engine and the reason. Every row in the report, and every marker note, says which engine made the call (`engine=jev/live`, `engine=opus/mock`, …).

| Engine | Decision types | When the engine is not there |
|---|---|---|
| Jev | `silence_gap`, `short_clip`, `filler_pause`, `long_static`, `colour_role`, `colour_aspect`, `take_keep`, `take_compare`, `cut_gate`, `pacing_violation`, `subtitle_break`, `audio_check` | The deterministic rules answer instead, at 0.85× confidence (`engine_source` `rules`). Never another model. A rules call is never `auto`. |
| Opus | `colour_unseen`, `story_structure`, `key_moments`, `music`, `typography`, `visual_treatment`, `montage`, `broll_selection` | The call becomes a review marker with no action (`engine_source` `unavailable`). Nothing is auto-applied. |

The engine decides. The gate still decides who may act: dialogue filler is a Jev call, and it stays in review because the pass is creative. Threshold comparisons inside the gate are arithmetic, so they stay code.

No Grok or xAI model is in the decision path. The room bots run commands and post reports. They do not make the calls. A Grok or xAI model id or URL in `CONDUCTOR_JEV_MODEL`, `CONDUCTOR_OPUS_MODEL`, or the host overrides is refused.

Calls are batched: one Jev request per 24 candidates (48 questions), one Opus request per 12. Answers are cached by content, not by id or position. So `iterate` round 2 only asks about regions that changed, and identical regions are asked once. The first failed request marks that engine down for the rest of the run. Every report has a `decision_usage` counter: calls per engine (live and mock), items, cache hits, fallbacks, and tokens and cost when the host returns them. The CLI prints it as a `decisions` line.

The assembly engine and future passes call the same router: `Router.decide([Ask(...)])`. See the docstring in `conductor/router.py`. A new decision type is a `register_decision(name, engine=..., question=..., why=...)`.

Subtitle line breaks (`subtitle_break`), how long a cue stays up (`subtitle_timing`), and whether a word an edit cut in half is shown (`subtitle_partial`) are Jev calls. Which section titles appear (`title_placement`) and which distortion each uses (`title_treatment`) are Opus calls. With no Opus answer the title is still placed, on the profile's default treatment, and the clip gets a review marker.

## The bot roster

| Bot | Job |
|---|---|
| **byjwu** | Build orchestrator. Merges code into this repo. |
| **Cut Conductor** | Runs edits. Runs `room-run` on each drop (which calls the `iterate` loop), posts `room.md`, and applies only cuts that passed the gate or that Justin accepted. |
| **Pacing** | Pace preferences and the `pacing` pass. |
| **Colour** | Colour, and the review-only `colour` pass. |
| **Style** | Owns the byjustinwu style profile. |
| **Type & Subs** | Transcripts, SF Pro subtitles, and text treatments. Drives the `dialogue` pass. |

The bots coordinate. The editorial decisions come from Jev and Opus. The contract the bots follow is the [room protocol](room-protocol.md).

## The loop

```text
~/Desktop/byjwu-in  (a folder, an export, a bundle, or a zip)
        →  room-run detects which
        →  starter sequence when the input is a folder of clips
        →  iterate: analyze, then auto-apply only mechanical cuts the gate allows
        →  ~/Desktop/byjwu-out/<name>-<time>/vN  and room.md
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
python -m conductor room-run ~/Desktop/byjwu-in/cut.fcpxml \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-root ~/Desktop/byjwu-out
```

The same command takes a `.fcpxml`, a `.fcpxmld` bundle, a `.zip` of either, or a folder of clips. It detects which, and it does not modify the drop. Dry-run is the default. `--live` is how a bot calls Jev. An SRT or WebVTT sitting next to the timeline is picked up; `--transcript` overrides that. A `durations.json` in a clip folder is picked up the same way.

When the drop also carries music (`.mp3`, `.wav`, `.aif`, `.m4a`, and similar), room-run hands it to a style assembler first (`--style`, default `byjustinwu`) if one is installed, then runs the loop on what it built. Without one, the clips are handled as above and the summary says the music was not placed.

Each run writes a new folder, `~/Desktop/byjwu-out/<name>-<timestamp>/`, so repeating it is safe. `room.md` in that folder is the chat summary (input kind, duration before and after, cuts with timecodes, rows flagged for the editor, stop reason, which signals were available, and the file to open). `room.json` is the same summary. The shadow FCPXML is always there.

`python -m conductor room-run --watch` processes new drops in `~/Desktop/byjwu-in` after the copy has finished, and skips ones it has already recorded. Pass a folder after `--watch` to watch a different one. `--out-root` defaults to `~/Desktop/byjwu-out`. Both defaults fall back to the legacy folders as described above. Setup for that process is in [room-run.md](room-run.md). That note is for the person who runs the bot, not for the editor.

What `room-run` calls is the iterate loop:

```bash
python -m conductor iterate \
  --media ~/Desktop/byjwu-in \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --transcript ~/Desktop/byjwu-in/interview.srt \
  --out-dir ~/Desktop/byjwu-out \
  --max-rounds 5
```

An export instead of a folder uses `--fcpxml` and omits `--media`. One of those two inputs, not both. `room-run` chooses.

With neither `--fcpxml` nor `--media`, `iterate` reads `~/Desktop/byjwu-in`: a single `.fcpxml` there is treated as the export, otherwise the folder is the selects. Rounds then go to `~/Desktop/byjwu-out` unless `--out-dir` says otherwise. The folder names live in `conductor/folders.py`, along with the legacy fallback.

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

When the bot machine can read a referenced media file, quiet stretches inside a clip are also mechanical candidates, and a local transcript can feed dialogue and pacing. If the file or the tool is missing, that stage is skipped and the XML-only run still finishes. What the bot should install, and the `signals` fields to quote, are in the [room protocol](room-protocol.md#media-signals).

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

**Relink.** Each `media-rep` `src` is an absolute `file://` URL, the path on the machine that ran ingest. Final Cut can open the media when that path resolves. If you generated the XML somewhere else, or the volume is not mounted, use **File → Relink Files…**. byjwu does not copy media into a library.

**Durations.** `--durations` wins when it names the file (`8`, `8s`, or `1/8s`). Otherwise the engine runs ffprobe. If ffprobe is missing or cannot read the file, that clip is **10 seconds** in the XML. That placeholder is not the picture's length. Final Cut will use the 10s written in the XML until you set a real duration and run again. Install ffmpeg so ffprobe is on `PATH` before you ingest a real folder.

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
| `colour` | A spine clip with no role. An asset frame that badly mismatches the sequence (portrait against landscape, or about 15% off), with any rotation or scale already in the XML. A placeholder for exposure and skin. | Review or escalate. The picture is not decoded. Never an unattended cut, and never a grade of the pixels. |
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

The log is not a training set. It becomes a per-kind prior: rejections lower confidence and can only make the mechanical auto gate stricter. Accepts may raise confidence, and they cannot newly open auto-apply unless a standing rule sets `loosen_auto`. The report says why, in a sentence on the row (`taste_reason`). A re-export diff and a notes file are how that log gets written. The room contract is in [docs/room-protocol.md](room-protocol.md). See `fixtures/taste.json` and `fixtures/feedback/`.

```bash
python -m conductor feedback \
  --taste fixtures/taste.json \
  --out out/taste.json \
  --event reject --id c0004 --action tighten --pass dialogue \
  --note "keep the breath"
```

Each run can also write `*.conductor.json` (the room payload), `*.conductor.html` (`--html`), and `*.taste.json`. Marker color is the `color=` field of the note, because FCPXML markers have no color attribute. Review and escalate markers are to-dos (`completed="0"`). A marker’s `start` is in the clip’s source time.

Roles, the accept loop, and the payload fields are in [docs/room-protocol.md](room-protocol.md).

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

The room bots (see [the bot roster](#the-bot-roster)) are how a cut moves. Justin drops a path in `~/Desktop/byjwu-in` and opens whatever lands in `~/Desktop/byjwu-out` when the room asks. The software in this repo is the engine and the CLI those bots call. The contract is [docs/room-protocol.md](room-protocol.md): who owns which pass, how `iterate` stops, and how a re-export or a chat note becomes a prior on the next gate.

The Cut Conductor bot runs `python -m conductor room-run`. That command detects the drop and calls `iterate` (a clip folder is ingested as the starter sequence, then iterated). It does not invent a sixth action. It does not apply a cut the gate did not allow unless a person accepted that id. Unattended cuts are mechanical only. Paste `room.md` into the room. The per-round JSON report (`protocol` `cut-conductor.room`) is still the state behind each round. `iterate.json` (`protocol` `cut-conductor.iterate`) is the stop record. `room.json` (`protocol` `cut-conductor.room-run`) is the chat summary. Bots do not re-sort those lists.

When the input is a folder, the room is woken with the starter FCPXML path, the inventory, the brief, and the media paths. Not with the media bytes.

The bots are orchestration, not a decision engine. A call about the cut goes through the router to Jev or Opus, and the bot posts what came back.

## Roadmap

In progress:

- **Real-export hardening.** Parse and write back real Final Cut exports, not only the checked-in fixtures.
- **Media signals.** Quiet audio inside a clip, local transcripts, and word timings are read when the bot machine can open the media (see the [room protocol](room-protocol.md#media-signals)). Picture signals are not built: the `colour` pass still has an honest placeholder where exposure and skin would need the picture.
- **Taste learning.** Per-kind priors already shift later confidence from rejections, accepts, and editor re-exports. They do not train a model, and they do not loosen mechanical auto-apply unless a rule opts in. Learning from Justin's published videos belongs to the style profile below.
- **Room run and watcher.** `room-run` and `room-run --watch` are built. With the launchd example in [room-run.md](room-run.md), an operator keeps the `~/Desktop/byjwu-in` inbox running. The editor still does not run a command. Still FCPXML out, still no plugin.
- **Style-driven assembly.** Build the sequence from raw footage and music according to the style profile. Today ingest is filename order, and a brief does not reorder clips.
- **Jev/Opus decision router.** Built: bounded, logical calls go to Jev, and open-ended creative and taste calls go to Opus 5.5 (see [The decision router](#the-decision-router)). The Opus decision types beyond the colour placeholder wait on style-driven assembly and future passes to ask them.
- **byjustinwu style profile.** Typography, pacing, colour, SF Pro subtitles, the rectangle background layer, and music fades and ducking, learned from his YouTube videos. The Style bot owns it. The graphics stage reads it. The numbers that stage ships with today are placeholders.

Later:

- **More passes.** `colour` is a review-only scaffold: roles and aspect from the XML. `story`, `audio`, and `broll` are still reserved. A new check is a `register_pass`, not a new product.
- **Renames.** The GitHub repo may move to the byjwu name. The legacy drop-folder fallback can go once no machine still uses the old folders.

## Type and graphics

`conductor.graphics.apply_graphics` is the stage `iterate`, `room-run`, and a future `conductor.assemble` call. It is off unless `--graphics` is passed or the style profile sets `graphics.enabled`. It writes onto the output FCPXML only. The drop is not modified.

Three layers, all generated by code:

| layer | what it is | who decides |
|---|---|---|
| Subtitles | `cut-conductor.words` grouped into phrases, as Final Cut Basic Title elements on a lane above the primary storyline. The active word is a bolder run in the same title | Jev: line break, on-screen time, and words an edit cut part way through. Group size follows Diffusion Studio's caption `groupBy` |
| Titles | Section cards. `none` and `scale_warp` are Basic Titles. `scale_warp` is `keyframeAnimation` on the title's scale. Glitch slice, RGB split, wave, and blur-in stay transparent movies, because Final Cut cannot do that distortion | Opus: whether the title appears, and which treatment |
| Rectangles | A seeded Shapes generator on a connected lane: position and scale keyframes, Gaussian blur, and a hue shift. The same seed still drives `rect_schedule` | The profile. Placement and blend come from `rect_layer` |

Rendered movies go in `<name>.assets/` next to the output FCPXML. Each `media-rep` `src` is a relative path, so it resolves when the folder is at `~/Desktop/byjwu-out/<name>/` on Justin's Mac. The rectangle blend mode and opacity are `adjust-blend` on the connected clip (Final Cut's numeric modes: Screen is 10, Add is 8).

The schema and the placeholder defaults live in `conductor/graphics/profile.py`: `typography`, `subtitles`, `text_fx`, and `rect_layer`. **Those defaults were not measured from Justin's videos.** They are SF Pro Display and SF Pro Text, clean white, with a subtle shadow, so the stage can run before the style study fills the profile in. A profile file overrides a field by a `graphics` object or by a measured `params` path listed in `PARAM_MAP`. `GraphicsResult.placeholder_fields` names whatever is still a placeholder, and that note is written into `room.md`.

SF Pro is not on Linux. Rendered titles use the first installed face in the fallback list, and the title XML still names SF Pro, so Final Cut uses it on the Mac. If ffmpeg or Pillow is missing, that render is skipped and the run continues. Subtitles do not need either. `CONDUCTOR_FFMPEG=off` forces the skip. HEVC with alpha is only written where macOS VideoToolbox exists; everywhere else the movie is ProRes 4444.

`--beats` is a JSON list of seconds, or `{"beats": [...]}`. Nothing in the media-signal stage produces a beat grid yet. Without one, and with `beat_sync` on, the layer still places shapes and the note says it did not cut to a beat.

### Borrowed from Diffusion Studio

[diffusionstudio/core](https://github.com/diffusionstudio/core) is a TypeScript, browser-only WebCodecs compositor. Unlicensed builds watermark the picture, so it is not a dependency and nothing is rendered through it. `conductor/graphics/diffusion.py` copies two algorithms, credited in that file:

- Caption `groupBy`: pack words by count, by the sum of each word's spoken duration, or by character length. A word that would pass the limit opens the next group. Subtitles use the character limit (`max_chars_per_line * max_lines`) and then the spoken-duration limit (`max_seconds`). A spine cut or a phrase pause still splits a group, so a line never crosses an edit. The active word is a separate `text-style` run (Diffusion Studio's WHISPER preset dims the words that are not the one being said).
- Keyframe lerp: before the first frame and after the last, the value clamps; between frames it is a linear mix, with a smoothstep when the interpolation is `smooth`. Title `scale_warp` and the rectangle position/scale tracks are that lerp, written as FCPXML `keyframeAnimation`.

Rectangle blur and hue-rotate are the same two effects Diffusion Studio applies as CSS filters on `RectangleClip`. Here they are a Gaussian filter and a Hue/Saturation filter on the Shapes generator, so the layer stays editable in Final Cut.

`iterate --graphics` and `room-run --graphics` call the stage once, on the timeline the summary names. `assemble` calls `apply_graphics` itself on the timeline it writes, passing the run's `router`. A timeline that already has a `byjwu ` asset is left alone.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
python scripts/ingest_dry_run.py
python scripts/iterate_dry_run.py
```

No API key. Engine tests cover the parser, marker write-back, the mock client, the gates, apply, ingest (including the fixture folder and the local page), the colour pass, iterate (two rounds, the round cap, and a duration window), `room-run` (an FCPXML, a `.fcpxmld` bundle, a zip, a clip folder, bad drops, and the watcher), the byjwu drop folders with their legacy fallback, and the graphics stage (subtitles that stay inside a clip and do not overlap, a deterministic rectangle layer, and a clean skip when ffmpeg is missing). The placeholder clips under `fixtures/selects/` are a few bytes each.

`tests/test_conductor_router.py` covers the decision router against fake Jev and Anthropic hosts (`httpx.MockTransport`). It checks the classification, attribution on rows and markers, the rules fallback and the review fallback, bad or refused Opus answers, batching, the cache across runs and iterate rounds, the call counter, and that no key reaches a report.

## Also in this repo: cutmcp

cutmcp is a separate MCP server: raw interview footage in, an EDL out, five tools (`ingest`, `estimate`, `cut`, `review`, `export_timeline`). It is not the byjwu engine and it is not a sixth pass. Install and tool docs: [docs/cutmcp.md](cutmcp.md).

The Python project is `byjwu`. It ships both packages, `conductor` and `cutmcp`. For Final Cut, run `python -m conductor`.

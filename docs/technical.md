# byjwu: technical docs

Everything about how byjwu works and how to run it. The short version is in the [README](../README.md).

byjwu helps Justin ([@byjustinwu](https://www.youtube.com/@byjustinwu) on YouTube) edit his YouTube videos using TypeSafe Jev and Claude Opus 5.5. A full Grok Bot orchestration, a room of specialist bots, works out everything stylistic and taste-related about his editing: typography, pacing, style, colours, subtitles, digital assets (the abstract rectangle background layer), and music fades and ducking.

The goal is raw footage and music in, and a finished FCPXML out that imports into Final Cut Pro and feels like a byjustinwu video. The style is learned from his published YouTube videos and gets better each time he re-exports a corrected cut.

Every Jev call has a confidence and a receipt. Nothing here is remote control of the Final Cut window. The handoff is FCPXML.

The engine module is still called `conductor` and still runs as `python -m conductor`, and the `cutmcp` / `cut-conductor` console scripts are unchanged. Those names stay so work already in flight keeps merging. Cut Conductor is now the name of the room bot that runs edits. Links to the GitHub repo still use `github.com/justinwuzijin/jevid` until Justin renames it.

## How Justin uses it

Justin does not use the command line. The room bots do.

1. He drops a selects folder, or a Final Cut **File → Export XML…** file, in the Grok Bot room or in `~/Desktop/byjwu-in`.
2. The Cut Conductor bot runs `python -m conductor iterate` on that path.
3. Each round is written under `~/Desktop/byjwu-out` (`v1/`, `v2/`, …). The applied FCPXML in the last round that cut something is the cut so far.
4. He steps in when a row is an escalate, or the loop hits its round cap. Review markers stay on the timeline. The loop does not apply them.
5. He opens the FCPXML in Final Cut Pro himself (**File → Import → XML…**). Import creates a new event. It does not patch the project he already has open.

FCPXML goes in and FCPXML comes out. `~/Desktop/byjwu-in` and `~/Desktop/byjwu-out` are ordinary folders on the machine the bot runs on. Nothing in this repo watches the Desktop, uploads picture or sound, or drives Final Cut. The bot reads a path and writes a new file. If `byjwu-in` / `byjwu-out` don't exist but the older `jevid-in` / `jevid-out` do, the engine uses the legacy folders and prints a one-line note. A watcher is still in progress (see [Roadmap](#roadmap)).

A folder of clips becomes a starter sequence first (filename order, absolute `file://` paths), then the same loop. An export is iterated as it stands. Source clips and the file he dropped are only read.

## Who decides what

| Piece | Decides | Does not |
|---|---|---|
| **Jev** (TypeSafe) | Linear, logical, bounded calls: keep, tighten, remove, mark for review, or escalate, each with a confidence and a risk. It picks from options the code defines (`conductor/jev.py`). | Write text, or make open-ended taste calls. |
| **Claude Opus 5.5** | Open-ended creative and taste calls, and graphics built through code (text treatments, the rectangle background layer). | Generate video. Opus does not generate video natively. |
| **Grok Bot room** | Coordination: routes work, runs the engine, posts paths and reports, and asks Justin when a call needs him. | Editorial work. No Grok model makes an editing decision. |
| **Final Cut Pro** | The timeline is the truth. Justin imports the FCPXML byjwu writes, and exports XML when he already has a cut. | — |

Jev's calls are live in the engine today. Opus taste calls go through the Jev/Opus decision router, which is in progress. The note on a marker is assembled afterwards from the action, the confidence, and the reason. No model writes it.

## The bot roster

| Bot | Job |
|---|---|
| **byjwu** | Build orchestrator. Merges code into this repo. |
| **Cut Conductor** | Runs edits. Runs the `iterate` loop (or a single `analyze` / `ingest` shadow pass), posts the report, and applies only cuts that passed the gate or that Justin accepted. |
| **Pacing** | Pace preferences and the `pacing` pass. |
| **Colour** | Colour, and the review-only `colour` pass. |
| **Style** | Owns the byjustinwu style profile. |
| **Type & Subs** | Transcripts, SF Pro subtitles, and text treatments. Drives the `dialogue` pass. |

The bots coordinate. The editorial decisions come from Jev and Opus. The contract the bots follow is the [room protocol](room-protocol.md).

## The loop

```text
~/Desktop/byjwu-in  (a folder, or an FCPXML export)
        →  starter sequence when the input is a folder
        →  iterate: analyze, then auto-apply only mechanical cuts the gate allows
        →  ~/Desktop/byjwu-out/vN
        →  stop when the metrics hold, when nothing mechanical is left, or at the round cap
        →  a person, only for escalate or max rounds
        →  you open the FCPXML in Final Cut
        →  a re-export or a note in the room updates taste, and the next gate moves
```

On the checked-in interview, round 1 lifts the long silence. The “um”, the hold, and the colour placeholder stay marked and are not cut. Round 2 finds no further mechanical cut and stops. The export you made in Final Cut is still sitting there, unchanged. No plugin was attached.

## Commands the room runs

From the repo root. No API key. These are the commands a bot shells out to. The desktop folders above are the paths it passes.

```bash
pip install -e ".[dev]"
```

The loop the room owns:

```bash
python -m conductor iterate \
  --media ~/Desktop/byjwu-in \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --transcript ~/Desktop/byjwu-in/interview.srt \
  --out-dir ~/Desktop/byjwu-out \
  --max-rounds 5
```

An export instead of a folder uses `--fcpxml ~/Desktop/byjwu-in/cut.fcpxml` and omits `--media`. One of those two inputs, not both. Dry-run is the default. `--live` is how a bot calls Jev.

With neither `--fcpxml` nor `--media`, `iterate` reads `~/Desktop/byjwu-in`: a single `.fcpxml` there is treated as the export, otherwise the folder is the selects. Rounds then go to `~/Desktop/byjwu-out` unless `--out-dir` says otherwise. The folder names live in `conductor/folders.py`, along with the legacy fallback.

Each round writes `~/Desktop/byjwu-out/vN/`: a shadow FCPXML, and an applied FCPXML only when a mechanical auto-gate cut landed. The next round reads the applied file, or the shadow when nothing was cut. `iterate.json` in the output folder is the stop record: the per-round metrics, the cuts, and `stop_reason` (`metrics`, `no-progress`, or `max-rounds`).

Stop when every metric you set is true, when a round applies nothing, or at `--max-rounds` (default 5). Metrics are optional. Leave them unset and the loop runs until the mechanical cuts run out or the cap hits.

| flag | stops when |
|---|---|
| `--target-seconds` + `--tolerance` (default 1s) | duration is inside that window |
| `--max-escalate` | escalate rows are at or under the cap |
| `--max-review` | review rows are at or under the cap |
| `--max-silence-seconds` | silence-gap candidates sum to at most this |
| `--min-shot-seconds` | average non-gap spine clip is at least this long |
| `--max-cuts-per-minute` | joins between spine shots, per minute, are at or under this |

Silence is the sum of gap and hole candidates of at least 1.25s. It is not a decoded quiet measurement. Shot length and cuts per minute are read off the spine. If the timeline is already inside the metrics, the round does not cut.

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

Read `cut.conductor.md`. The companion `cut.conductor.fcpxml` is the same edit with proposal markers added. Import it into a **duplicate event** if you want those markers in Final Cut. Importing creates a new project. It does not patch the one you exported.

The transcript is optional SRT or WebVTT. Times are the sequence clock, not the source clip.

To call live Jev, copy `.env.example` and pass `--live`. Dry-run stays the default even when a key is present. `CONDUCTOR_DRY_RUN=1` forces the mock anyway.

| Variable | Host | Model |
|---|---|---|
| `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` |
| `TYPESAFE_API_KEY` | `POST https://api.typesafe.ai/v1/systemone` | `jev-1.13.0` |

OpenRouter wins when both are set. `CONDUCTOR_JEV_PROVIDER=typesafe` forces the other. The TypeSafe host rejects the slug `jev-1.13`, so the client sends `jev-1.13.0` there.

`python -m conductor` is the command to use from the repo. A `cut-conductor` script is installed with the package and may land outside your `PATH`.

## Passes, gates, apply, taste

Passes run in order — `mechanical`, then `dialogue`, then `pacing`, then `colour` — or one at a time with `--pass`. `iterate` judges that same set and auto-applies only `mechanical`.

| Pass | Looks for | Who may apply it |
|---|---|---|
| `mechanical` | Silence of at least 1.25s. Clips under 0.45s (under 0.20s is a flash). | High-confidence tighten/remove, with `--min-confidence`. This is what `iterate` auto-applies. |
| `dialogue` | A whole filler cue (0.25–3s), or a pause of at least 0.80s beside filler | Review, unless you `--accept` the id |
| `pacing` | A clip of at least 20s under 0.40 words/second. With no transcript: a hold/slate/b-roll name, or a clip of at least 45s | Review, unless you `--accept` the id |
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

The Cut Conductor bot runs `python -m conductor iterate` (or `analyze` / `ingest` for a single shadow pass). It does not invent a sixth action. It does not apply a cut the gate did not allow unless a person accepted that id. Unattended cuts are mechanical only. The per-round JSON report (`protocol` `cut-conductor.room`) is the state the room posts. `iterate.json` (`protocol` `cut-conductor.iterate`) is the stop record. Bots do not re-sort either list.

When the input is a folder, the room is woken with the starter FCPXML path, the inventory, the brief, and the media paths. Not with the media bytes.

## Roadmap

In progress:

- **Real-export hardening.** Parse and write back real Final Cut exports, not only the checked-in fixtures.
- **Media signals.** Measurements taken from the picture and the sound, passed to decisions as structured state. The `colour` pass has an honest placeholder where exposure and skin would need the picture.
- **Taste learning.** Per-kind priors already shift later confidence from rejections, accepts, and editor re-exports. They do not train a model, and they do not loosen mechanical auto-apply unless a rule opts in. Learning from Justin's published videos belongs to the style profile below.
- **Room run and watcher.** `iterate` is the loop the room runs today, on paths the bot is given. A watcher on `~/Desktop/byjwu-in`, or a Finder drop, that starts a run with no terminal is not built. The local page is a browser on 127.0.0.1. Still FCPXML out, still no plugin.
- **Style-driven assembly.** Build the sequence from raw footage and music according to the style profile. Today ingest is filename order, and a brief does not reorder clips.
- **Jev/Opus decision router.** Bounded, logical calls go to Jev. Open-ended creative and taste calls go to Opus 5.5.
- **byjustinwu style profile.** Typography, pacing, colour, SF Pro subtitles, the rectangle background layer, and music fades and ducking, learned from his YouTube videos. The Style bot owns it.

Later:

- **More passes.** `colour` is a review-only scaffold: roles and aspect from the XML. `story`, `audio`, and `broll` are still reserved. A new check is a `register_pass`, not a new product.
- **Renames.** The GitHub repo may move to the byjwu name. The legacy drop-folder fallback can go once no machine still uses the old folders.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
python scripts/ingest_dry_run.py
python scripts/iterate_dry_run.py
```

No API key. Engine tests cover the parser, marker write-back, the mock client, the gates, apply, ingest (including the fixture folder and the local page), the colour pass, and iterate (two rounds, the round cap, and a duration window). The placeholder clips under `fixtures/selects/` are a few bytes each.

## Also in this repo: cutmcp

cutmcp is a separate MCP server: raw interview footage in, an EDL out, five tools (`ingest`, `estimate`, `cut`, `review`, `export_timeline`). It is not the byjwu engine and it is not a sixth pass. Install and tool docs: [docs/cutmcp.md](cutmcp.md).

The Python project is `byjwu`. It ships both packages, `conductor` and `cutmcp`. For Final Cut, run `python -m conductor`.

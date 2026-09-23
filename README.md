# jevid

A Grok Bot room that uses Jev to propose Final Cut cuts — and, when a call is solid, apply those cuts. Every call has a confidence and a receipt. The handoff is FCPXML. You open the file the bot gives you in Final Cut.

The editor's only step is a drop in that room. The Conductor bot runs the program and posts a path back.

Not an auto-editor of the open library. Not remote control of the Final Cut window.

## Who it’s for

Final Cut editors who want a decision layer and a small crew around a cut: someone on pace, someone on the transcript, and someone who will not change the timeline unless the call is solid or a person said yes in the room.

## The only editor step

1. Drop one FCPXML export, or a folder of clips, into the Cut Conductor room.
2. On a Mac the conventional folder is `~/Desktop/jevid-in`. The Conductor bot reads it and writes `~/Desktop/jevid-out`.
3. The bot posts the handback path (or attaches that FCPXML). Open it in Final Cut with **File → Import → XML…**.
4. Answer in the room when a call was escalated, or when the bot says the loop hit its round limit. Those are the moments a person decides.

Optional files in the same drop, beside the export or the clips:

| file | what the bot does with it |
|---|---|
| `brief.txt` | What this cut is for. |
| one `.srt` or `.vtt` | Transcript on the sequence clock. |
| `taste.json` | Pace preferences and the accept/reject log. |
| `durations.json` | File name to length, when the drop is clips and ffprobe should not guess. |

One `.fcpxml`, or video files. A drop with both is refused. Subfolders and dotfiles are ignored. Containers: `mov`, `mp4`, `m4v`, `mxf`, `avi`, `mkv`, `mts`, `m2ts`.

Import creates a new event. It does not patch a project you already have open, and it does not change the clip files.

## What the bot runs

The commands below are for the Conductor bot and for people developing it. An editor uses the room.

After a drop, the bot runs `iterate`. A folder of clips is ingested into a starter sequence first. An export is analyzed as it stands. Each round:

1. Shadow-marks every pass (mechanical, dialogue, pacing).
2. Auto-applies only mechanical calls that clear the confidence gate. Dialogue and pacing stay in review.
3. Writes `v1/`, `v2/`, … under the output folder. The next round reads the applied file when that round cut, otherwise the shadow file.
4. Stops when the metrics clear, when a round applies nothing and the metrics are unchanged, when `--max-rounds` is hit (default 5), or when a round errors.

Default stop metrics, all read from the timeline and the report: total silence at or under 1.25s, open escalations at 0. A duration target is used only when one was set (within 2 seconds). Open reviews, average shot length, and cuts per minute are reported every round and only block a stop when the bot sets a bound.

```bash
python -m conductor iterate --drop ~/Desktop/jevid-in
```

That writes `~/Desktop/jevid-out` when the folder is named `jevid-in`. The bot posts `iterate.md` and the handback path from `iterate.json`. It posts paths, not picture or sound.

A morning with an export already in the drop: the long silence can leave as a mechanical cut the gate allows. The “um” and the long hold stay marked for the room, and they are not cut. The file you exported is still sitting in `jevid-in`, unchanged. A folder of clips takes the same path after the starter sequence is written.

### From a folder of clips

`ingest` is the step `iterate` runs when the drop is clips. Developers can run it on its own. No API key.

```bash
pip install -e ".[dev]"
python scripts/ingest_dry_run.py
```

The checked-in fixture is three placeholder files, not playable media. Durations are in `fixtures/selects/durations.json`. The script writes `out/selects-dry-run/`. The same command, spelled out:

```bash
python -m conductor ingest \
  --media fixtures/selects \
  --durations fixtures/selects/durations.json \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-dir out/selects-dry-run
```

What the bot gets:

| file | what it is |
|---|---|
| `Selects.fcpxml` | starter sequence. Filename order, one spine, no transitions. |
| `Selects.conductor.fcpxml` | the same sequence plus proposal markers. |
| `Selects.conductor.applied.fcpxml` | only with `--apply`, and only for cuts the gate allows. |
| `Selects.conductor.md` / `.json` | the ranked list. The JSON includes an `ingest` object: paths, durations, and a hash of each clip. |

`Selects` is the folder name. `--sequence` replaces it. With no brief, the run uses a filename-order assembly line.

**Relink.** Each `media-rep` `src` is an absolute `file://` URL, the path on the machine that ran ingest. Final Cut can open the media when that path resolves. If the XML was generated somewhere else, or the volume is not mounted, use **File → Relink Files…**. jevid does not copy media into a library.

**Durations.** `--durations` wins when it names the file (`8`, `8s`, or `1/8s`). Otherwise Conductor runs ffprobe. If ffprobe is missing or cannot read the file, that clip is **10 seconds** in the XML. That placeholder is not the picture's length. Final Cut will use the 10s written in the XML until a real duration is supplied and the bot runs again. Install ffmpeg so ffprobe is on `PATH` before a real folder is ingested.

**What the folder scan does.** This folder only, not subfolders. Dotfiles are skipped. Order is the file name, case-insensitive. The brief does not reorder clips. When ffprobe can read a file, that file's frame size and rate are written on its asset; the sequence format follows the first clip. Unprobed clips are 1920×1080, 24fps.

Shadow markers are the default. The bot opts into an applied file with the same gates as `apply`:

```bash
python -m conductor ingest --media ~/Desktop/jevid-in --brief "..." \
  --apply --min-confidence 0.8 --pass mechanical --out-dir ~/Desktop/jevid-out
```

`--accept` and `--min-confidence` do nothing unless `--apply` is also set. Creative passes are not cut by the confidence switch.

A local page exists for developers who want to check a path on this machine:

```bash
python -m conductor ui
```

Open `http://127.0.0.1:8765`. Type the folder path, or drop a `file://` path. **Choose folder…** asks the OS for a directory when a display is available. The form does not upload video. Editors use the room, not this page.

### From an existing cut

```bash
python scripts/dry_run.py
```

That runs the checked-in interview and writes `out/sample-dry-run/`. On an export the bot was given:

```bash
python -m conductor analyze cut.fcpxml \
  --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-dir ~/Desktop/jevid-out
```

`iterate --fcpxml` is the loop around that same analyze. Read `cut.conductor.md`. The companion `cut.conductor.fcpxml` is the same edit with proposal markers added. Import it into a **duplicate event** if those markers should show up in Final Cut. Importing creates a new project. It does not patch the one that was exported.

The transcript is optional SRT or WebVTT. Times are the sequence clock, not the source clip.

To call live Jev, copy `.env.example` and pass `--live`. Dry-run stays the default even when a key is present. `CONDUCTOR_DRY_RUN=1` forces the mock anyway.

| Variable | Host | Model |
|---|---|---|
| `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions` | `typesafe/jev-1.13` |
| `TYPESAFE_API_KEY` | `POST https://api.typesafe.ai/v1/systemone` | `jev-1.13.0` |

OpenRouter wins when both are set. `CONDUCTOR_JEV_PROVIDER=typesafe` forces the other. The TypeSafe host rejects the slug `jev-1.13`, so the client sends `jev-1.13.0` there.

`python -m conductor` is the command the bot uses from the repo. A `cut-conductor` script is installed with the package and may land outside `PATH`.

## Passes, gates, apply, taste

Passes run in order — `mechanical`, then `dialogue`, then `pacing` — or one at a time with `--pass`.

| Pass | Looks for | Who may apply it |
|---|---|---|
| `mechanical` | Silence of at least 1.25s. Clips under 0.45s (under 0.20s is a flash). | The bot, when confidence clears the gate |
| `dialogue` | A whole filler cue (0.25–3s), or a pause of at least 0.80s beside filler | A person in the room, who names the id |
| `pacing` | A clip of at least 20s under 0.40 words/second. With no transcript: a hold/slate/b-roll name, or a clip of at least 45s | A person in the room, who names the id |
| `story`, `audio`, `broll` | Not built | `conductor.passes.register_pass` |

Filler matches the whole cue (`um`, `you know`, `i mean`, and the same list cutmcp uses). `like`, `yeah`, and `okay` are not filler.

Jev returns a raw action, a confidence, and a risk. The gate decides what happens next. Defaults:

| | Rule | Result |
|---|---|---|
| Auto | Confidence ≥ 0.80, risk ≤ 0.35, and the pass is mechanical | The iterate loop may cut it |
| Review | Confidence ≥ 0.55, or any creative pass | To-do marker. A person may accept the id |
| Escalate | Confidence below 0.55, or Jev said escalate | To-do marker. The room asks a person |

Creative passes never take the auto disposition, no matter how sure the model is. Thresholds can be overridden in a taste file under `gates`. The numbers in a dry-run come from a local mock so a fixture can be read without a key. They are not evidence that live Jev is calibrated. Treat live confidence as unproven until it has been checked on real marks.

Apply writes a **second** FCPXML. The bot passes ids a person accepted, or a confidence floor and a pass. `ingest --apply` uses this same rule. `iterate` always uses the mechanical confidence path and leaves creative passes marked.

```bash
python -m conductor apply cut.fcpxml --transcript cut.srt \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --min-confidence 0.8 --pass mechanical --out-dir ~/Desktop/jevid-out

python -m conductor apply cut.fcpxml --transcript cut.srt \
  --brief "..." --accept c0003 --out-dir ~/Desktop/jevid-out
```

`cut.conductor.applied.fcpxml` is the cut. `cut.fcpxml` is untouched. `--min-confidence` on dialogue or pacing matches nothing, because those passes are creative. The bot names the id a person accepted.

A `tighten` on a whole clip keeps the first `hold_seconds` (default 4) and lifts the tail. A `remove`, a filler, or a hole lifts that range and closes the gap. A transition on the spine is refused rather than left at a stale offset.

Taste is a JSON file: `jump_cut_tolerance`, `target_pace` (`tight`, `measured`, `loose`), `cold_open_bias` (`keep`, `neutral`, `cut`), `hold_seconds`, plus an accept/reject log. The bot passes it with `--taste`. The prefs, the last 20 events, and the fingerprints already applied go into Jev’s state. Nothing is trained on the log. See `fixtures/taste.json`.

```bash
python -m conductor feedback \
  --taste fixtures/taste.json \
  --out ~/Desktop/jevid-out/taste.json \
  --event reject --id c0004 --action tighten --pass dialogue \
  --note "keep the breath"
```

Each run can also write `*.conductor.json` (the room payload), `*.conductor.html` (`--html`), and `*.taste.json`. Marker color is the `color=` field of the note, because FCPXML markers have no color attribute. Review and escalate markers are to-dos (`completed="0"`). A marker’s `start` is in the clip’s source time.

`iterate` carries the taste file from round to round. An applied cut is stored with a fingerprint (pass, kind, clip name, duration, span) so a later round does not treat that same region as a new cut. Candidate ids (`c0001`) are renumbered every round and are only valid inside that round’s JSON.

Roles, the accept loop, and the payload fields are in [docs/room-protocol.md](docs/room-protocol.md).

## Safety

- The default command is `analyze`. `ingest` without `--apply` does not cut. `iterate` cuts only mechanical autos.
- `apply`, and `ingest --apply`, error unless the caller passes `--accept`, or both `--min-confidence` and `--pass`.
- The confidence path only cuts `auto` rows. Creative calls stay in review or escalate.
- Output paths that resolve to the source FCPXML, or to a source clip, are refused. The source bytes are checked at the end of the run.
- Ingest hashes each clip before and after. A change aborts the run.
- There is no Final Cut plugin and no live control of the open library.
- The local page binds to 127.0.0.1 and rejects multipart uploads.

## The Grok Bot room

Conductor, Pacing, and Transcript are how a person drives this. The software they call is the library and the CLI. The contract is [docs/room-protocol.md](docs/room-protocol.md): who owns which pass, how a drop becomes a handback, and how accept/reject events land in taste for the next decide call.

A Conductor bot runs `python -m conductor iterate` on `~/Desktop/jevid-in` (or `analyze` / `ingest` for a single pass). It does not invent a sixth action. It does not apply a cut the gate did not allow unless a person accepted that id in the room. The JSON report (`protocol` `cut-conductor.room`, and `cut-conductor.iterate` for the loop) is the state the room posts. Bots do not re-sort it.

When the input is a folder, the room is woken with the starter FCPXML path, the inventory, the brief, and the media paths. Not with the media bytes.

## Roadmap

- **Ordering.** Ingest is filename order. A brief does not reorder clips yet.
- **Taste.** Applied fingerprints are already in the decide state so iterate does not re-cut them. Shifting later calls from the rest of the log is not built.
- **More passes.** `story`, `audio`, and `broll` are reserved. A new check is a `register_pass`, not a new product.
- **Room drop.** The editor drops into the Cut Conductor room. The Mac folders are `~/Desktop/jevid-in` and `~/Desktop/jevid-out`. A Finder app that watches those folders by itself is not in this repo. The bot is what runs.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
python scripts/ingest_dry_run.py
```

No API key. Conductor tests cover the parser, marker write-back, the mock client, the gates, apply, ingest (including the fixture folder and the local page), the drop folder, and iterate. The placeholder clips under `fixtures/selects/` are a few bytes each.

## Also in this repo: cutmcp

cutmcp is a separate MCP server: raw interview footage in, an EDL out, five tools (`ingest`, `estimate`, `cut`, `review`, `export_timeline`). It is not the Final Cut co-pilot and it is not a sixth pass. Install and tool docs: [docs/cutmcp.md](docs/cutmcp.md).

The Python project name on disk is still `cutmcp`, so an editable install keeps working. For Final Cut, the bot runs `python -m conductor`.

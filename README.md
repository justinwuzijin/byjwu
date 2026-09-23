# jevid

A Grok Bot room that uses Jev to propose Final Cut cuts — and, when you accept them, apply those cuts. Every call has a confidence and a receipt. The handoff is FCPXML. You open the result in Final Cut yourself.

Not an auto-editor. Not remote control of the Final Cut window.

## Who it’s for

Final Cut editors who want a decision layer and a small crew around a cut: someone on pace, someone on the transcript, and someone who will not change the timeline unless the call is solid or a person said yes.

## How it fits together

| Piece | What it does |
|---|---|
| **Final Cut Pro** | The timeline is the truth. You import the FCPXML jevid writes. You export XML when you already have a cut. |
| **Cut Conductor** | The program in this repo. Named passes, confidence gates, proposal markers, and an explicit apply. |
| **Jev** (TypeSafe) | Typed decisions only: keep, tighten, remove, mark for review, or escalate. It does not write the marker text. |
| **Grok Bot room** | Conductor, Pacing, and Transcript. They explain the list, ask a person, and follow the [room protocol](docs/room-protocol.md). |

Jev picks from options the code defines. The note on a marker is assembled afterwards from the action, the confidence, and the reason.

## Two ways in

Both end the same way: an FCPXML you import into Final Cut. Neither one edits the open library, and neither one posts picture or sound to a webhook.

**A. An existing cut.** In Final Cut, choose **File → Export XML…** and run Cut Conductor on that file.

**B. A folder of clips.** Point `--media` at the folder (and, if you have them, a brief and a transcript). Conductor writes a starter sequence in filename order, then runs the same passes. A page on your machine can do that from a path. The files stay on disk.

## The loop

```text
FCPXML export, or a folder of clips
        →  starter sequence when the input is a folder
        →  mechanical, dialogue, pacing
        →  Jev: action + confidence + risk
        →  shadow markers and a ranked list
        →  you accept an id, or a high-confidence mechanical cut qualifies
        →  a new FCPXML
        →  you open it in Final Cut
        →  accept / reject is logged for the next pass
```

A morning with an export: you run a dry-run. The long silence shows up as a mechanical cut the gate would allow. The “um” and the long hold show up for you to look at, and they are not cut. If you want the markers on a timeline, you import the shadow XML into a duplicate event. When you agree with the silence cut, you apply it to yet another file. The export you made in Final Cut is still sitting there, unchanged. No plugin was attached. A folder of clips takes the same path after ingest writes the starter sequence.

## Quickstart

From the repo root. No API key.

```bash
pip install -e ".[dev]"
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

Passes run in order — `mechanical`, then `dialogue`, then `pacing` — or one at a time with `--pass`.

| Pass | Looks for | Who may apply it |
|---|---|---|
| `mechanical` | Silence of at least 1.25s. Clips under 0.45s (under 0.20s is a flash). | High-confidence tighten/remove, with `--min-confidence` |
| `dialogue` | A whole filler cue (0.25–3s), or a pause of at least 0.80s beside filler | Review, unless you `--accept` the id |
| `pacing` | A clip of at least 20s under 0.40 words/second. With no transcript: a hold/slate/b-roll name, or a clip of at least 45s | Review, unless you `--accept` the id |
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

`cut.conductor.applied.fcpxml` is the cut. `cut.fcpxml` is untouched. `--min-confidence` on dialogue or pacing matches nothing, because those passes are creative. Name the id.

A `tighten` on a whole clip keeps the first `hold_seconds` (default 4) and lifts the tail. A `remove`, a filler, or a hole lifts that range and closes the gap. A transition on the spine is refused rather than left at a stale offset.

Taste is a JSON file: `jump_cut_tolerance`, `target_pace` (`tight`, `measured`, `loose`), `cold_open_bias` (`keep`, `neutral`, `cut`), `hold_seconds`, plus an accept/reject log. Pass it with `--taste`. The prefs and the last 20 events go into Jev’s state. Nothing is trained on the log. See `fixtures/taste.json`.

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

- The default command is `analyze`. `ingest` without `--apply` does not cut.
- `apply`, and `ingest --apply`, error unless you pass `--accept`, or both `--min-confidence` and `--pass`.
- The confidence path only cuts `auto` rows. Creative calls stay in review or escalate.
- Output paths that resolve to the source FCPXML, or to a source clip, are refused. The source bytes are checked at the end of the run.
- Ingest hashes each clip before and after. A change aborts the run.
- There is no Final Cut plugin and no live control of the open library.
- The local page binds to 127.0.0.1 and rejects multipart uploads.

## The Grok Bot room

Conductor, Pacing, and Transcript are how a person is meant to drive this. v1 of the software is the library and the CLI they call. The contract is [docs/room-protocol.md](docs/room-protocol.md): who owns which pass, how a shadow run becomes an accepted cut, and how accept/reject events land in taste for the next decide call.

A Conductor bot runs `python -m conductor analyze` or `python -m conductor ingest` (or imports `conductor.analyze` / `conductor.ingest`). It does not invent a sixth action. It does not apply a cut the gate did not allow unless a person accepted that id. The JSON report (`protocol` `cut-conductor.room`) is the state the room posts. Bots do not re-sort it.

When the input is a folder, the room is woken with the starter FCPXML path, the inventory, the brief, and the media paths. Not with the media bytes.

## Roadmap

- **Ordering.** Ingest is filename order. A brief does not reorder clips yet.
- **Taste.** The log is already in the decide state. Learning from it — actually shifting later calls — is not built.
- **More passes.** `story`, `audio`, and `broll` are reserved. A new check is a `register_pass`, not a new product.
- **A Mac drop helper.** The local page is a browser on 127.0.0.1. A Finder drop that never opens a terminal is not built. Still FCPXML out, still no plugin.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
python scripts/ingest_dry_run.py
```

No API key. Conductor tests cover the parser, marker write-back, the mock client, the gates, apply, and ingest (including the fixture folder and the local page). The placeholder clips under `fixtures/selects/` are a few bytes each.

## Also in this repo: cutmcp

cutmcp is a separate MCP server: raw interview footage in, an EDL out, five tools (`ingest`, `estimate`, `cut`, `review`, `export_timeline`). It is not the Final Cut co-pilot and it is not a sixth pass. Install and tool docs: [docs/cutmcp.md](docs/cutmcp.md).

The Python project name on disk is still `cutmcp`, so an editable install keeps working. For Final Cut, run `python -m conductor`.

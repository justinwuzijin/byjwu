# jevid

A Grok Bot room that uses Jev to propose Final Cut cuts — and, when you accept them, apply those cuts. Every call has a confidence and a receipt. The handoff is FCPXML. You open the result in Final Cut yourself.

Not an auto-editor. Not remote control of the Final Cut window.

## Who it’s for

Final Cut editors who want a decision layer and a small crew around a cut: someone on pace, someone on the transcript, and someone who will not change the timeline unless the call is solid or a person said yes.

## How it fits together

| Piece | What it does |
|---|---|
| **Final Cut Pro** | The timeline is the truth. You export FCPXML, and you import the file jevid writes back. |
| **Cut Conductor** | The program in this repo. Named passes, confidence gates, proposal markers, and an explicit apply. |
| **Jev** (TypeSafe) | Typed decisions only: keep, tighten, remove, mark for review, or escalate. It does not write the marker text. |
| **Grok Bot room** | Conductor, Pacing, and Transcript. They explain the list, ask a person, and follow the [room protocol](docs/room-protocol.md). |

Jev picks from options the code defines. The note on a marker is assembled afterwards from the action, the confidence, and the reason.

## Two ways in

**A. An existing cut.** In Final Cut, choose **File → Export XML…** and run Cut Conductor on that file. This works now.

**B. A folder of selects.** Drop clips and a brief, get a starter sequence, then the same passes. Not in this build. The planned shape is a local `ingest` command and a small page on your machine: files stay on disk, and the room is woken with paths and metadata, not the picture or the sound. Nothing in that design posts media to a webhook.

Both ways are meant to end the same way: an FCPXML you import into Final Cut.

## The loop

```text
FCPXML (or, later, a folder of selects)
        →  mechanical, dialogue, pacing
        →  Jev: action + confidence + risk
        →  shadow markers and a ranked list
        →  you accept an id, or a high-confidence mechanical cut qualifies
        →  a new FCPXML
        →  you open it in Final Cut
        →  accept / reject is logged for the next pass
```

A morning with it: you export the interview and run a dry-run. The long silence shows up as a mechanical cut the gate would allow. The “um” and the long hold show up for you to look at, and they are not cut. If you want the markers on a timeline, you import the shadow XML into a duplicate event. When you agree with the silence cut, you apply it to yet another file. The export you made in Final Cut is still sitting there, unchanged. No plugin was attached.

## Quickstart

From the repo root. No API key.

```bash
pip install -e ".[dev]"
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

Apply writes a **second** FCPXML. It does nothing unless you name ids or set a confidence floor and a pass:

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

- The default command is `analyze`. It does not cut.
- `apply` errors unless you pass `--accept`, or both `--min-confidence` and `--pass`.
- The confidence path only cuts `auto` rows. Creative calls stay in review or escalate.
- Output paths that resolve to the source file are refused. The source bytes are checked at the end of the run.
- There is no Final Cut plugin and no live control of the open library.

## The Grok Bot room

Conductor, Pacing, and Transcript are how a person is meant to drive this. v1 of the software is the library and the CLI they call. The contract is [docs/room-protocol.md](docs/room-protocol.md): who owns which pass, how a shadow run becomes an accepted cut, and how accept/reject events land in taste for the next decide call.

A Conductor bot runs `python -m conductor analyze` (or imports `conductor.analyze`). It does not invent a sixth action. It does not apply a cut the gate did not allow unless a person accepted that id. The JSON report (`protocol` `cut-conductor.room`) is the state the room posts. Bots do not re-sort it.

When ingest exists, the room will be woken with the starter FCPXML path, the inventory, the brief, and the media paths. Not with the media bytes.

## Roadmap

- **Ingest.** A folder of selects, or a small local page, becomes a starter FCPXML (filename order first; brief-driven order later), then the same passes. Files stay on disk.
- **Taste.** The log is already in the decide state. Learning from it — actually shifting later calls — is not built.
- **More passes.** `story`, `audio`, and `broll` are reserved. A new check is a `register_pass`, not a new product.
- **A Mac drop helper.** So an editor can drop a folder or an export without opening a terminal. Still FCPXML out, still no plugin.

## Tests

```bash
python -m pytest
python scripts/dry_run.py
```

No API key. Conductor tests cover the parser, marker write-back, the mock client, the gates, and apply.

## Also in this repo: cutmcp

cutmcp is a separate MCP server: raw interview footage in, an EDL out, five tools (`ingest`, `estimate`, `cut`, `review`, `export_timeline`). It is not the Final Cut co-pilot and it is not a sixth pass. Install and tool docs: [docs/cutmcp.md](docs/cutmcp.md).

The Python project name on disk is still `cutmcp`, so an editable install keeps working. For Final Cut, run `python -m conductor`.

# room-run for bot operators

This note is for the person who runs the Grok bot on the editor's Mac. The editor does not run these commands. They drop a file in `~/Desktop/jevid-in` and open the FCPXML the room names.

`room-run` is the one command the bot should shell out to. It detects the drop, calls `iterate`, and writes a folder the bot can describe in chat.

## One drop

```bash
python -m conductor room-run ~/Desktop/jevid-in/cut.fcpxml \
  --brief "A tight interview. Keep the guest's story, lose dead air." \
  --out-root ~/Desktop/jevid-out
```

Accepted inputs:

| drop | what room-run does |
|---|---|
| `.fcpxml` | iterate that timeline |
| `.fcpxmld` bundle (`Info.fcpxml` inside) | iterate that timeline |
| `.zip` of either | unzip into the output folder, then iterate |
| folder of clips | starter FCPXML, then iterate |

The drop is only read. Each run creates `~/Desktop/jevid-out/<name>-<YYYYMMDD-HHMMSS>/`. Running it again creates another folder.

`room.md` is the chat text: input kind, flow, duration before and after, cuts with timecodes, rows flagged for the editor, stop reason, signals (`transcript`, `media`, `music`, or `none`), and the absolute path to open in Final Cut. `room.json` is the same object (`protocol` `cut-conductor.room-run`). Paste `room.md`. Do not re-sort the flagged list; it follows the round report. Cut timecodes are positions on the timeline before that cut. Flagged times are positions on the file to open.

A shadow FCPXML is always written. The path in the summary is the timeline to import (the last round's cut when that round cut something, otherwise the shadow, which already includes earlier cuts and the markers).

Dry-run is the default. No API key is read. `--live` calls Jev and needs `OPENROUTER_API_KEY` or `TYPESAFE_API_KEY` in the environment. Do not put those keys in a plist, a shell script you commit, or the chat.

An `.srt` or `.vtt` beside the timeline is used when there is one obvious file. `--transcript` overrides it. A clip folder's `durations.json` is used when it is there. `--durations` overrides it.

## Music and the assembly hook

A drop that carries music goes through an assembler before the loop, when one is installed. Music is `.mp3`, `.wav`, `.aif`, `.aiff`, `.m4a`, `.aac`, `.flac`, or `.caf`: files at the top of a clip folder, loose files in the inbox next to loose clips, or assets a dropped FCPXML points at.

| assembler available | what runs | `flow` in the summary |
|---|---|---|
| yes | assembler with `--style` (default `byjustinwu`), then iterate its FCPXML | `assemble+iterate` |
| no | the usual path; a warning says the music was not placed | `ingest+iterate` or `iterate` |

room-run looks for `conductor.assemble.assemble`, or a callable passed to `conductor.room.register_assembler`. It passes only the keywords the callable's signature names, from: `media`, `fcpxml`, `music`, `style`, `brief`, `out_dir` (`<run>/assemble/`), `live`, `transcript_path`, `taste_path`, `durations_path`. The callable returns the FCPXML it wrote, as a path, a mapping, or an object with `fcpxml`, `out_fcpxml`, `timeline`, or `path`. The drop is still only read, and the summary still names the file to open.

## Failures the bot can paste

`room-run` exits 2 and prints one sentence. Paste that sentence. The same text is in `room.md` of the timestamped folder when the run got far enough to create one.

- A zip with no Final Cut XML.
- An FCPXML version other than 1.8 through 1.14.
- A clip folder with no video files, or a timeline whose media was supposed to be inside the drop and is not there.

Media that the XML names on another volume is not a failure. The summary says the run used the timeline only, and Final Cut may need Relink Files.

## Watch the inbox

```bash
python -m conductor room-run --watch ~/Desktop/jevid-in \
  --out-root ~/Desktop/jevid-out \
  --brief "A tight interview. Keep the guest's story, lose dead air."
```

The process scans the top of `jevid-in`. A file or folder is processed only after its size has stayed the same for `--stable-seconds` (default 2). That waits out a copy that is still writing. Already processed bytes are stored in `~/Desktop/jevid-out/.room-run.json` and skipped. Results are appended to `~/Desktop/jevid-out/room-run.log`.

Loose video files dropped straight into the inbox are one clip-folder run. A `.fcpxml`, a `.zip`, a `.fcpxmld`, or a subfolder is its own run. Sidecar `.srt`, `.vtt`, `durations.json`, and music files are not runs; they travel with the loose clips.

A drop that fails for a reason other than bad input (a read error, a crash in a step) is logged and recorded like any other failure. The watcher keeps running.

Run one watcher. A second process will fight the first for the same drops.

Stop with Ctrl-C. A launchd job exits when the process is stopped and, with `KeepAlive`, starts again.

## launchd

`docs/com.jevid.room-run.plist` is an example. Replace every `CHANGE_ME` path. Install it for the macOS user who owns `~/Desktop`, so the paths resolve as that person:

```bash
cp docs/com.jevid.room-run.plist ~/Library/LaunchAgents/com.jevid.room-run.plist
# edit the copy, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jevid.room-run.plist
```

The example does not pass `--live` and does not set an API key. Leave it that way unless you have decided the room should call Jev. Logs from launchd go to `~/Library/Logs/jevid/`. The run log the bot can read is still `~/Desktop/jevid-out/room-run.log`.

Unload with:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.jevid.room-run.plist
```

# End-to-end style score

A synthetic shoot is built, cut with the dry-run room path, and scored against the measured `byjustinwu` profile. No real footage and no API key.

## How to run it

```bash
python3 scripts/synth_e2e_shoot.py /tmp/byjwu-shoot
python3 -m conductor room-run /tmp/byjwu-shoot \
  --out-root /tmp/byjwu-out \
  --style byjustinwu \
  --brief "A short diary about why an edit feels slow, and the one rule that fixes it."
python3 eval/style_score.py /tmp/byjwu-out/<run>/v2/timeline.conductor.applied.fcpxml \
  --style byjustinwu \
  --assembly /tmp/byjwu-out/<run>/assemble/assembly.json
```

The pytest that does the same thing is `tests/test_eval_e2e.py` (`slow`). `python3 -m pytest tests/ -q` runs it with the rest of the suite.

The generator writes ten clips and two click tracks: 48 seconds at 120 BPM, then 160 seconds at 96 BPM. Talking clips are longer takes, several sentences each, with a pause where the topic changes. They are test patterns plus a tone that is loud only while a line is spoken. Each of those clips has an SRT sidecar and a word-timed JSON sidecar. B-roll clips are silent pictures with names like `broll_train_platform`. There is no speech synthesizer on this machine, so the tone plus the sidecar is the transcript. The media files are created at test time and are not committed.

`room-run` sees the music file, calls the assembly engine, then iterates. Dry-run is the default, so Jev and the taste model stay on their local rules.

## Scores

Overall **97.6** out of 100. Lint passed. The critic approved. The pytest requires overall at least 88 and every dimension at least 80.

| Dimension | Score |
|---|---|
| Pacing | 100 |
| Hook | 100 |
| Hygiene (retakes and filler) | 100 |
| Beat | 100 |
| B-roll | 80 |
| Ducking | 100 |
| Type and chapters | 100 |
| Colour notes | 100 |
| Structure | 100 |

Pacing compares each section’s average shot length with the profile, inside the profile’s tolerance band. Beat is the share of montage cuts that landed within the profile’s snap window of a detected beat. The other rows are pass-or-partial checks: a kept opening line, dropped retakes, cutaways or montage shots, a ducked or silenced bed under talk, chapter markers and cards, a colour review note, and the section order.

## What changed

- An earlier take is dropped when a later line on the same clip repeats it or finishes a false start. A shorter echo after a finished line is left alone.
- A single music bed is not restarted at every section. That had been stacking copies of the file’s head on one lane.
- Rectangle graphics sit on free lanes, and a shape shorter than two frames is not written. Their durations are on the frame grid.
- Music fades written as volume keyframes count as head and tail fades.
- Effect generators are not treated as media, so a shapes layer is not a reused source range. A looped music bed may reuse its own file. Cutaways still may not.
- When a cut would delete a spine item that still has something attached, that cut is skipped instead of failing the run.
- Colour and other review notes are copied onto the applied timeline, which is the file the room says to open.
- Montage shots are held up to the short end of the profile’s average, when the chunk is long enough, so the section does not fill with flashes.
- Talking sentences from the same take stay one hold until they reach the profile’s talking average. A new segment from `conductor/segments.py` starts a new hold, so a topic shift is a cut.
- Montage cuts snap to the nearest beat within half a beat of the drawn length. The shot may run a little past that chunk into later unused source on the same clip. A later montage section does not replay source the first section already used. Speech cuts stay on the words.
- The FCPXML check now also rejects overlapping spine items, times off the frame grid, an asset-clip whose ref does not exist, and two items on the same lane that overlap.

## What is still weak

B-roll scores 80. The profile barely covers talk, so the montage is the B-roll, and the harness scores montage-only coverage at 80. Cutaway lengths are not what that row measures here.

The timeline is a few minutes because the shoot is ten clips. The profile’s length target is the long diary runtime. The score does not punish that gap. A real drop would.

There is no speech synthesizer here, so the voice is a tone. Word times come from the sidecar, not from listening.

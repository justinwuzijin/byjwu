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

The generator writes ten clips and two click tracks: 48 seconds at 120 BPM, then 160 seconds at 96 BPM. Talking clips are longer takes, several sentences each, with a pause where the topic changes. They are test patterns. When `espeak-ng` is on `PATH` the voice is synthesized speech; otherwise a tone is loud only while a line is spoken. Each talking clip also gets an SRT sidecar and a word-timed JSON sidecar unless the generator is asked not to write them. B-roll clips are pictures with names like `broll_train_platform`. The media files are created at test time and are not committed.

`room-run` sees the music file, calls the assembly engine, then iterates. Dry-run is the default, so Jev and the taste model stay on their local rules.

## Scores

Overall **95.8** out of 100 on the sidecar run. Lint passed. The critic approved. The pytest requires overall at least 88 and every dimension at least 80.

| Dimension | Score |
|---|---|
| Pacing | 100 |
| Hook | 100 |
| Hygiene (retakes and filler) | 100 |
| Beat | 100 |
| B-roll | 80 |
| Ducking | 81.8 |
| Type and chapters | 100 |
| Colour notes | 100 |
| Structure | 100 |

Pacing compares each section’s average shot length with the profile, inside the profile’s tolerance band. Beat is the share of montage cuts that landed within the profile’s snap window of a detected beat. Ducking reads the music level under dialogue on the timeline. A bed that the profile would silence does not score as ducked unless the file is actually quiet there. The other rows are pass-or-partial checks: a kept opening line, dropped retakes, cutaways or montage shots, chapter markers and cards, a colour review note, and the section order.

## Bad edits of the same shoot

`tests/test_style_discrimination.py` builds three edits from the synthetic media without the assembler.

| Edit | Overall |
|---|---|
| (a) Every clip laid end to end, no cuts | 20.7 |
| (b) Short pieces at 0.75s, off the beat | 20.7 |
| (c) Dead air and retakes kept, the bed restarted from the file head, no ducking | 22.0 |

(a) and (b) are under 60. (c) is ducking 0, hygiene 35, hook 45. The 97.6 cut is not a score any of these can reach.

## Speech, no sidecars

With `espeak-ng` and `faster-whisper` `base.en`, the same shoot was generated with no `.srt` and no word-timed JSON. Assembly calls the local whisper path in `conductor/signals.py`. This run:

| Dimension | Score |
|---|---|
| Overall | 96.6 |
| Pacing | 100 |
| Hook | 100 |
| Hygiene | 100 |
| Beat | 100 |
| B-roll | 80 |
| Ducking | 90.0 |
| Type and chapters | 100 |
| Colour notes | 100 |
| Structure | 100 |

Lint passed. The critic approved. `tests/test_eval_e2e.py::test_no_sidecar_room_run_matches_the_style` is the same path. It is marked slow and skips when `espeak-ng` or `faster-whisper` is missing.

`base.en` is the default. It is the Systran CTranslate2 conversion of the OpenAI Whisper English weights (MIT), about 150 MB. On a laptop CPU, int8 is a few times faster than realtime, which is enough for a full shoot. `tiny` mis-hears espeak and splits holds. `small.en` is more accurate, but it is several times slower and about 500 MB, which is the wrong trade for a CPU pass over a whole folder. If `base.en` is already in the Hugging Face cache it is used even when `tiny` is cached too. If nothing is cached, it is downloaded once and a log line records the cache directory. `CONDUCTOR_WHISPER_MODEL` set to a missing name does not trigger that download.

If `faster-whisper` is not installed, `room-run` says to install it with `pip install 'byjwu[whisper]'` (the `whisper` extra in `pyproject.toml`). Silence ranges stay the network-free fallback.

## Drop folder

`tests/test_room_drop_shapes.py` is a folder of raw clips plus one music file and no FCPXML. It includes 23.976, 29.97, and 59.94, a portrait clip, and a clip with no audio. Dry-run `room-run` with no API keys writes an FCPXML under a new timestamped folder. A second call writes a different folder.

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
- One song shorter than the timeline loops on a bar with a short crossfade. The next copy's beats are loaded before the montage shot that would otherwise land past the file. A longer song fades out at the timeline end.

## What is still weak

B-roll scores 80. The profile barely covers talk, so the montage is the B-roll, and the harness scores montage-only coverage at 80. Cutaway lengths are not what that row measures here.

The timeline is a few minutes because the shoot is ten clips. The profile’s length target is the long diary runtime. The score does not punish that gap. A real drop would.

The 95.8 number is the sidecar run, where word times come from the SRT. Ducking is 81.8 because the score reads the music level under dialogue, and a few spans are not all the way down at the profile's silent bed. The 96.6 number is the no-sidecar run: `base.en` word times, then the same assembler. A take's picture range is reserved once, so the head of a clip is not laid twice. Holds group on pause length, the segment index, and staying on the same take, so a misheard word does not open a new shot. A silence sliver beside a music bed is left in the shot, so a montage cut stays on the beat.

## One song

Justin usually drops one track. The two-song shoot above hides a hole: when the only file is shorter than the timeline, the next copy used to start at the current playhead, which was already past the file end. A 48 second bed left gaps after each copy. A 160 second bed left a gap from 160.0 to 161.04.

The bed now loops on the last downbeat within one bar of the file end, with a short crossfade, and the beat grid for the next copy is in place before a montage shot is drawn. Ducking is written on every copy. A song longer than the timeline is one clip and fades out at the end. A song about as long as the timeline covers it and fades out the same way.

`tests/test_single_song.py` assembles those three beds with sidecars and checks the linter finds no `music_coverage` hole. `tests/test_eval_e2e.py::test_one_short_song_room_run_matches_the_style` is the drop-folder path: ten espeak clips, no sidecars, only `bed_120bpm.wav` (48 seconds), dry-run, no keys.

That run scored overall **96.6**. Lint passed. The critic approved.

| Dimension | Score |
|---|---|
| Pacing | 100 |
| Hook | 100 |
| Hygiene | 100 |
| Beat | 100 |
| B-roll | 80 |
| Ducking | 90.0 |
| Type and chapters | 100 |
| Colour notes | 100 |
| Structure | 100 |

Iterate ran on the assembled timeline and applied 2 cuts. When it applies none, the report says the assembly had already made the cut, rather than reading as if the pass was skipped.

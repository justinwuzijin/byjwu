# byjustinwu: style guide

Measured from three long video journals and one short. The short is a different
genre and only informs typography. Pooled numbers use the three long videos.
Every number here is in `profile.json` with its confidence, and
`study/analyze.py` recomputes it from a local bundle. Evidence notes are
aggregate stats (`median of 5 chapter cards`, `n=89 sampled frames`). Video
ids stay in the gitignored `study/data/` folder.

## In one paragraph

A diary, not a highlight reel. It opens on talk to the lens with no music,
stamps a date and time of day within the first 13 seconds, then lets music
carry b-roll while the story is told in **silent lowercase subtitles**, not
voice-over. Talk is unhurried: long takes, jump cuts, real pauses, and no
music underneath. Music comes back in blocks for montages that are mixed very
loud. Chapters are marked by a burst of pixel-glitch colour, a collage of
flat and warped rectangles, a run-together lowercase title ending in `;` or
`*` (for example `weekone;`), and a two-line date stamp. The big moments get
one full-frame, vertically stretched all-caps slam. It ends on a brand card.

## Structure

1. **Cold open, no title.** Two of three long videos open on talk to camera
   (100.6 s and 9.8 s). One opens on 12.8 s of music over a single shot.
   Nothing opens on a title card.
2. **Date stamp in the first 13 s.** A right-aligned two-line stamp, for
   example `weekone;` over `noon`. The stamp is the establishing shot.
3. **Brand in the open (2 of 3).** A full-frame wordmark over the opening
   talk, or a small glitched wordmark. A tagline in the form `a {noun}
   byjustinwu` lands a few seconds later.
4. **Chapters.** About 3.1 per 10 minutes, median 178.5 s (16 chapters,
   85-358 s).
5. **Inside a chapter** the sections alternate: talking or handheld footage
   with live sound, then music-led b-roll or a montage.
6. **Reflective close.** One of: a voice-over monologue over a held bed
   (69.4 s), a music-only montage (244.1 s), or narrated b-roll (54.5 s).
7. **End card, median 9.7 s.** A tagline over a quiet shot, a stacked
   wordmark with rectangles, or a colour wash. Two of three finish with a
   1.4-1.7 s centred wordmark sting on a flat field (`#2016b3` with a hard
   black offset shadow, or orange caps on black).

## Cut rhythm

Hard cuts at scene threshold 0.3. The thr 0.15 column also counts whip pans
and motion blur, so read it as how busy the picture feels.

| section | what it is | median shot | p10-p90 | cuts/min (0.3 / 0.15) |
|---|---|---|---|---|
| talking | to the lens, live sound | 12.9 s (5.4 s with jump cuts) | 1.6-51 s | 2.2 / 6.0 |
| confessional | webcam talk to camera | 8.1 s | 2.1-51 s | 2.5 |
| vlog | handheld, live sound | 8.3 s | 2.0-31.5 s | 3.9 / 12.2 |
| b-roll | music-led, often narrated | 3.7 s | 1.7-9.6 s | 10.1 / 19.5 |
| montage | music only | 2.75 s | 1.0-6.6 s | 15.3 / 37.3 |
| monologue | voice-over over a bed | 3.4 s | 2.6-9.5 s | 11.2 |

- **Talk holds.** Longest single take 91.9 s. Jump cuts come about 3.7 a
  minute and are left raw, with no punch-in. Pauses are kept: 4.1 silences
  of half a second or more per minute of talk, 16 a minute in the webcam
  confessional.
- **Montage speed.** Fastest 8-shot run about 1.1 s per shot. One video has
  strobe bursts of 11-12 cuts in about 1.3 s.
- **Beats.** Montage cuts do not line up with loudness onsets any better than
  cuts shifted by 0.5-2 s (ratio 0.90, p 0.64-0.73 per video). Limited
  masters hide kicks in the data, so this means "not detectable", not "off
  the beat". `cut_on_beat` stays `prefer`.
- **Speech over cuts.** In the monologue 77% of cuts have speech on both
  sides. In talking sections it is 48%. True J/L offsets cannot be read from
  a mixed master.
- **Transitions** are hard cuts. None of 426 shot-start frames shows a
  dissolve. The only soft joins are the glitch field before chapter cards
  and one colour wash.

## Music and mix

- **Music plays in blocks between talk.** Pauses in on-camera talk measure
  -34 to -70 LUFS in two of three videos: room tone, no bed. Confessional
  pauses sit near -61 LUFS. One video keeps the previous track under talk,
  ducked about 15 dB.
- **The one measurable duck** drops 14.8 dB, starts 0.69 s before the first
  word, and finishes inside one 400 ms window. That is a keyframed pre-duck.
  Treat it as a single sample.
- **Monologue bed** sits flat 10.2 LU under montage and does not swell when
  the voice-over stops.
- **Levels** (median momentary): montage -8.0 LUFS, narrated b-roll -13.7,
  monologue -16.2, handheld -25.0, talking -29.8, confessional -36.8.
  Integrated loudness median -11.7 LUFS. True peak reaches +2.3 dBTP.
- **Starts and stops.** Music comes in at the first b-roll after the opening
  talk, or from frame 0 when the open is b-roll. A muted opening is silence,
  not a fade. Ramps are short: median 1.0 s in (n=6 boundaries, p10 0.3,
  p90 3.2) and 1.65 s out. Tail fades run 0.6-1.4 s, except one 13 s colour
  wash.
- **Lyrics are fine.** Narration is silent text, so vocal tracks never fight
  it.
- **Chapter cards are quiet.** Median of 5 cards plays about 16.5 LU under
  montage, with no music hit.

**Where the profile departs from the measurement:** talking is mixed at -18
LUFS rather than the measured -29.8, and true peak is capped at -1 dBTP
rather than the measured overs. The contrast (talk clearly under montage) is
the style.

## Typography

**Narration captions** (4 read lines, plus lines over music-led b-roll).

- Written narration, not transcription. 4 of 4 read lines have no speech
  under them.
- Neutral sans, white, no box, no visible stroke.
- Bottom centre: text top at 0.850 H, baseline at 0.872 H.
- One line, never wrapped, up to about 60% of frame width (48-83 characters,
  median 71).
- Lowercase. Proper nouns keep their case.
- Each line holds about 4 s, back to back, and can hold across a cut.
- Present in 24% of sampled b-roll frames. Absent on on-camera talk (0 of 89
  sampled frames) and on pure montage (0 of 21).

**Monologue captions.**

- A transitional serif, band 0.0153 H, baseline 0.8708 H, smaller than
  narration.
- Verbatim with the voice-over, lowercase. Continuation lines start with
  `-- `.

**Spoken captions** appear only on the short: same sans and position as
narration, verbatim. The long videos never caption on-camera talk.

**Chapter titles** (median of 5 cards in one long video).

- Large open grotesque, white, lowercase, set straight. X-height about
  0.12-0.13 H, so an em near 0.24 H.
- Words run together and end in `;` or `*`, for example `weekone;`.
- An aside in asterisks can sit beside a title. The words are the editor's.

**Date stamps** (n=9).

- Two right-aligned lines in the narration sans. 7 of 9 are a two-line block.
  Every long video stamps within its first 13 s.
- On full cards the stamp sits about 0.06 H tall. As an inset over live
  footage it rides a pair of textured rectangles (~0.2-0.3 W) for 3-6 s.
- The engine fills stamps from clip creation time.

**Slam text.**

- Full-bleed ALL CAPS, vertically stretched. Glyph height over advance is
  about 3.3-4.1 on the stretched cases (measured 4.12, 3.3, 1.03, 1.69).
- White, red `#d40000` / `#d70000`, or black on white. On screen 1-3 s.
- Used for the brand open, a thesis line, and a punchline. At most one or
  two per long video besides the brand open.
- The end sting is the same idea, smaller, on a flat field for 1.4-1.8 s.

**Serif section titles** (one of the three long videos).

- A lettered serif title over a date line, set over footage beside one
  textured rectangle. Example shape: `(a) weekone;` over `//month 00; morning`.

**Tagline.** `a {noun} byjustinwu` in the narration sans, left edge about
0.57-0.61 W, baseline 0.753-0.76 H.

## The rectangle layer

Outlines stay axis-aligned. The texture inside is what is warped. Anatomy of
a full chapter card (5 cards):

- **Field.** A full-frame pixel-sort glitch in one hue family. It plays alone
  for 0.33-1.09 s (median 0.67 s) before the card elements pop on.
- **Rectangles.** Three or four. Heights 0.11-0.82 H, typical 0.28-0.65.
  Pixel ratios 0.75-3.4. They touch or bleed off the frame and overlap
  15-40%. About half are flat fills and half are textures (zigzag, ripples,
  liquid, contours, pixel-sort).
- **Timing.** Every one of those 5 cards holds exactly 5.714 s (137 frames)
  after the lead-in.
- **Motion.** Rectangles and text do not move while the card holds. They
  leave in a stagger.
- **Inset variant.** Two rectangles over live footage carrying the date stamp
  for 3-6 s.
- **Other uses.** A coarse pixel-mosaic rectangle, and one solid `#ec4302`
  frame that returns as the closing wash.

## Other signatures

- **Mixed formats, pillarboxed.** 4:3 clips sit in black bars on the 16:9
  timeline and are never cropped. The aspect mismatch is intended.
- **Ultrawide POV** with the lens distortion left in.
- **Screen as set.** The confessional can be a screen recording, UI left in.

## How the engine uses this

`profile.json` is in the assembly engine's `jevid.style` schema (v1). It
extends `base` and overrides only what differs.

- **Engine section kinds pool the finer labels.** Talking includes the
  webcam and handheld labels. Montage includes music-led b-roll. Intro is
  the cold open. Outro is the last section before the end card.
- **Music blocks are clip-gain beds.** A talk-led intro and on-camera talk
  sit at -96 dB. Montage is the reference level. Variants sit beside each
  bed: `when_bed`, `when_music_broll`, `when_monologue`.
- **Speech is not captioned** (`typography.subtitle.enabled: false`). The
  subtitle geometry is still specified, for narration. Narration text,
  chapter titles, asides and the tagline noun are the editor's. Jev and the
  taste model only select; they never write.
- **Evidence** for every value is in `provenance.evidence`. `measured` points
  into `study/measured.json`. `adjusted` marks a deliberate departure.
- **`decisions`** are the runtime calls. The loader warns once that the
  top-level list is kept but not read.
  - Linear calls go to Jev (`editorial: false`): section role, music under
    talk, beat lock, keep pause.
  - Creative calls go to the taste model (`editorial: true`): opening,
    chapter marker style, slam line, closing. The default is
    `grok-4.7-medium` (`CONDUCTOR_TASTE_MODEL`). A Claude id selects Opus.
    Opus is optional.

## What the data could not answer

- Exact typefaces, tracking, and shadow. Sans, serif, and grotesque are
  visual matches only.
- Animation curves of text and rectangles. Stills show pop-on, hold, and a
  staggered exit. `study/motion.py` measures excerpt crops when they are
  present in the gitignored data folder.
- Texture sources. The engine needs those assets supplied.
- Beat alignment on a limited master.
- J/L cuts and the duck curve. One duck event, from a mixed master.
- Music selection: genre, tempo, and source are unknown.
- Caption timing finer than the 2 s frame spacing.
- The colour grade. The colour pass stays review-only.

## Reproduce

```bash
pip install numpy pillow
# video ids live only in styles/byjustinwu/study/data/sources.json (gitignored)
python styles/byjustinwu/study/analyze.py --bundle /path/to/unzipped/bundle \
  --out styles/byjustinwu/study/measured.json
python -m pytest tests/test_byjustinwu_profile.py
```

`study/sections.json` holds the hand labels: section type per time range,
card timings, and which frames to measure. It names videos by alias
(`long_a`, `long_b`, `long_c`, `short`). The script maps those aliases to
ids from `study/data/sources.json`.

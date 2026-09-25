# byjustinwu: style guide

These long-form video journals talk to camera, move through a stretch of days,
and break into lowercase chapters with loud, glitchy title cards. This guide
is what `profile.json` encodes, in words an editor would use. Numbers come
from `study/analyze.py` over the local study bundle (gitignored; not in this repo).
Visual claims were read from frames and cite `<video id> <seconds>`.
Motion numbers come from `study/motion.py` over the local intro excerpts.

Sources, newest first. The 2026 look is weighted over the 2025 one.

| id | length | weight | role |
|---|---|---|---|
| `newest` | 15:10 | 0.35 | newest long-form; date-stamp chapters |
| `cards` | 19:04 | 0.35 | clearest chapter-card system |
| `older` | 18:33 | 0.20 | older serif cards, fastest bursts |
| `short` | 1:02 | 0.10 | short; loudness and cut rate only |

## The feel in one paragraph

Let him talk, then hit hard. Talking takes run long: median 7.4 s, often
30–100 s without a cut. B-roll moves at about 3.5 s a shot and speeds up to
under 2 s in bursts. Music is about 12 dB louder than his voice when it has
the floor, starts at full level on frame 0, and drops out completely under
chapter cards. On screen, everything is lowercase and small (lines of
narration over b-roll, date stamps, the "a … byjustinwu" sign-off). Then a
chapter card fills the frame with a huge stretched title over overlapping
rectangles filled with glitch, lava, halftone and wavy-line textures in
saturated digital colours. Graphics never glide: they pop on and off in a
single frame and stay put, and only the textures inside them move.

## 1. Cut rhythm

| section | shot median | p10–p90 | n | notes |
|---|---|---|---|---|
| talking | **7.4 s** | 2.1–38.7 s | 112 | per video 5.6 / 9.6 / 5.5 s; longest take 102.3 s (`cards`) |
| b-roll | **3.5 s** | 1.6–9.7 s | 231 | per video 3.6 / 3.9 / 3.4 s: very consistent |
| montage | **2.3 s** | 1.0–4.4 s | 31 | only `older` has runs that qualify (473.7–523.9 s at 1.5 s) |
| first 30 s | 3.2 s | 1.7–18.5 s | 19 | `newest` opens on a 13 s-median talking head instead |
| last 30 s | 3.1 s | 1.7–8.1 s | 19 | |

Overall cut rate is 5.9–9.5 hard cuts per minute (7.9 in the newest video).

- **Bursts, not montages.** The 2026 videos never string four or more short
  b-roll shots together under a 2.5 s median. Their fastest five-shot runs sit
  around 1.8 s a shot (`newest` 542–554 s,
  `cards` 12–22 s in the cold open). The 0.4 s-a-shot flurry at
  `older` 605–612 s is a one-off, about 13 s before the
  a hard stop.
- **Jump cuts in talking.** The first word lands about 0.3 s after a cut
  (median, p10 0.06 s). The cut comes about 0.2 s after the last word ends.
  Tight, but never clipped.
- **Voice across cuts.** 31% of cuts next to talking land within 0.15 s of a
  word, so his voice carries over the picture change. That is split edits or
  b-roll laid over narration. The captions can't tell whether it's a J or an
  L cut.
- **On the music, loosely.** Of 269 cuts in music sections, 13% land within
  ±100 ms of a loudness onset, against 6.2% by chance: 2.1× chance, and
  consistent across all four videos (1.9–2.2×). He favours accents but does
  not cut on a beat grid. The engine should snap a cut only when an onset is
  within ±100 ms.
- He speaks at 2.8 words per second in all three long videos.

## 2. Music and sound

| measure | value | evidence |
|---|---|---|
| fade in | **none**, full level on frame 0; later entries are hard cuts too | head ramp 0.0–0.2 s in all long-form videos |
| fade out | **~1 s** after a spoken or card ending; 15.8 s when it ends on a montage | `newest` 0.8 s, `cards` 1.4 s, `older` 15.8 s |
| music vs voice | music-only stretches **~12 dB** hotter | 12.9 / 8.8 / 12.4 dB |
| dialogue level | about −24.5 LUFS short-term | −24.1 / −25.2 (2026), −18.6 (2025) |
| music-only level | about −14.5 LUFS short-term | −12.3 / −16.7 (2026), −7.8 (2025) |
| integrated | about −12 LUFS, LRA 18–23 LU | wide dynamics by design |

- **How music enters in the intro.** Never with a fade:
  - `cards`: a spoken line sets the scene (ends 4.8 s). Music cuts in about 0.65 s
    after it ends, mid-shot (+12 dB in 0.3 s at ~5.45 s). It then steps up
    another 12 dB exactly on the 13.60 s cut, 2 s before the first written
    line appears.
  - `older`: music from frame 0 under a silent window shot.
  - `newest`: a dry talking-head open with no music until about 106 s.
- **Track changes.** In `older` the first track fades out over about
  5.5 s during an errand walk (23.6–29.2 s). About 7.5 s of room sound
  follows, then the next track slams in at −6 LUFS (+35 dB in 0.4 s) on the
  36.95 s cut to a wide establishing shot. Establishing wides carry the hits.
- **Ducking.** The stereo side channel carries the music, since his voice is
  near-mono. It sits 11–13 dB lower while he talks. The bed is already
  halfway down 1–2.5 s before the first word and comes back under the last
  phrase. The cleanest example, `cards` 30.1 s, is an 8 dB step 2.1 s
  before he speaks, then a slow sag of another ~3 dB. There's no sidechain
  pump.
- **Talking with no music.** In the 2026 videos about a fifth of talking
  shots contain a real sub −40 dB pause, which means no music bed under them
  (10/42, 8/40). The reflective talking-head open of `newest` (0–90 s) has 16 of
  those pauses. The 2025 video is wall-to-wall music (1/30).
- **Silent cards.** The signature sound move: every mid-video chapter card
  in `cards` goes to **true digital silence** for 1.7–4.8 s (median
  4.2 s), starting 0.7–1.3 s after the card appears (173.8–178.3,
  370.0–371.7, 465.2–470.0, 875.2–878.5, 1075.8–1080.0 s). `older`
  does the same on one mid-video card (629.9–631.8 s). The card lands with a short hit,
  then nothing, then the chapter starts on sound.
- His masters peak above 0 dBFS (+0.2 to +2.3 dBTP). Don't copy that; the
  engine limits to −1 dBTP.

## 3. Typography

Everything is **lowercase**. Caps appear only in the `@JUSTINWU` wordmark and
the one manifesto title.

**Subtitle lines, and where they go.** He does **not** subtitle himself
talking to camera. None of the three excerpts has text over to-camera
talking (`newest` 8–90 s, `cards` 32–90 s, `older`
51–90 s). The lines appear over b-roll in one of two roles:

- **Written narration.** He tells the story in text over music instead of
  speaking it. `cards` 15.7–26 s is three written lines while the
  auto-captions have no speech from 4.8 to 32.2 s. The `newest` 175 s
  line sits over song lyrics, and the `older` 403.9 s line over
  on-location chatter.
- **Voice-over captions.** The closing monologue of `newest`
  (850–910 s).

The look is the same for both: white, no box, no visible stroke, centred
with the baseline at 86% of frame height, one line only. The face is a
neutral grotesque that reads as SF Pro Text Regular. Cap height is about
2.6% of frame height, roughly 36–40 pt at 1080p. Lines run up to ~90
characters, usually with no full stop (one line has one), and numbers are
digits.

Each line is one sentence, held 3.3–5 s (about 15 characters per second of
hold). Lines switch on a single frame with no fade and carry across picture
cuts; the 19–24 s line spans three cuts. The first line enters on a cut.

**Date stamps** (right-aligned, two lines, semicolon at the end of line one):

Two layouts, both right-aligned. One is a weekday, month and day, then a
time word (`noon`, `late evening`): `newest` 6 s and 361.5 s. The other
is a numeric month, day and year, then a clock time, used for
every `cards` stamp (10, 172.8, 366.1, 464.5, 874.5, 1074.5 s). Stamps
sit in the right third, with the right edge at 86–95% of frame width. The
text overhangs the left edge of its rectangle. They are a touch larger than
subtitles. A stamp stays up 2–4 s (`newest` 2.80–6.67 s,
`cards` 9.76–11.80 s). It can enter on a cut or mid-shot and ignores
cuts once it's up.

**Chapter titles, 2026.** Huge lowercase sans, about 25% of frame height,
stretched tall rather than set in a condensed cut. Spaces are dropped and the
title ends in `;` or `*`, sometimes split on a slash. They're white and overlap the
rectangle edges.

**Chapter titles, 2025.** A serif (Times-like) with letter enumerators, a `//`
comment subline and a `#month day` date, on footage or on flat orange `#ec4302`. In motion
(`older` 12.79–15.97 s), the title sits perfectly still while the
line art behind it is re-cropped every frame, and the magenta block drops
out about 1.6 s in.

**Wordmark.** `@JUSTINWU` in heavy all caps, stretched to fill the frame
height, white over the first shot (`cards` 0–3.25 s). It doesn't
scroll, scale or move while the handheld shot moves under it, and it
vanishes in one frame just before the 3.34 s cut. At the end it's tiled
black on white in three rows (1138–1144 s; motion unseen). Variants are an
outlined orange `@BYJUSTINWU` over manga line art (`older` 12 s) and a
pixel `BYJUSTINWU` on orange (1105–1111 s).

**Sign-off.** `a {noun phrase} byjustinwu`: small, lower right (starting at x 0.61, baseline 0.75). It
appears just after the cold open (`older` 16–28 s) or on the final shot
(`newest` 906–908 s).

**Manifesto title.** Stacked all-caps, red,
filling the frame before the closing monologue (`newest` ~852 s). One
instance, so it's a special move, not a rule.

**Distortion treatments seen.** Non-uniform vertical stretch, dropped word
spaces, outlined type over line art, pixelated logo type, and titles that
break past the rectangles behind them. No rotation, skew, per-letter jitter
or animated tracking. The one moving type effect is a pixel break-up: the
orange `BYJUSTINWU` and its lime rectangle shatter into blocks for about
0.25 s (`older` 10.1–10.35 s) before the rectangle drops away.

## 4. Background layer: the rectangles

- **Shape.** Axis-aligned rectangles with square corners, overlapping and
  offset, often bleeding off the frame edge. The contortion is in what fills
  them, not their outlines. No rotation was seen.
- **How many.** 1–2 behind a date stamp, 3–4 on a chapter card.
- **Size.** 15–45% of frame width and height. The date-stamp rectangle is
  about 0.21 × 0.34 (`newest` 6 s) or 0.30 × 0.40 (`cards` 10 s).
- **Fills:**
  - liquid iridescent gradient (`newest` 6 s)
  - pixel-sorted glitch crops and lava texture (`cards` 10 s)
  - wavy stripes (172.8 s) and topographic lines (874.5 s)
  - halftone dots (366.1 s)
  - perspective checkerboard (`newest` 361.5 s)
  - black-and-white manga map line art (`older` 12 s)
  - 8-bit pixel sprites (`newest` ~882 s)
  - flat solids
- **Palette** (dominant colours sampled from cards): electric blue `#390ee8`,
  hot pink `#f659a4`, red-pink `#cd3253`, magenta `#c00dcc`, acid lime
  `#d9fb07`, green `#08e413`, sky `#98cdff`, plum `#512f49`, graphite
  `#404143`, and the 2025 orange `#ec4302`. Each card sticks to one hue
  family plus a complementary accent.
- **Card background.** A full-frame pixel mosaic in the card's hue: lime
  (172.8 s), pink (366.1 s), violet (463.7 s), yellow (873.4 s), pale blue
  (1074.2 s).
- **Texture source.** The glitch rectangles at `cards` 10 s carry the
  orange of his jacket in the shot behind them, so they're likely
  pixel-sorted crops of the footage. The taste model builds these in code as graphics;
  nothing generates video.
- **Motion.** Measured in the intro excerpts:
  - **Position** is static: no drift, scale, rotation or tracking while the
    footage moves underneath.
  - **Entry** is a pop at full size in one frame. `cards` brings both
    rectangles and the stamp in together on the 9.76 s cut; `older`
    staggers by a few frames (lime rectangle 9.69 s, type 9.81 s; sign-off
    15.98 s, `FILM` sticker 16.23 s).
  - **Exit** is a pop too, one layer at a time from the top of the stack,
    2–3 frames apart, text last. `cards`: red 11.68 s, yellow
    11.76 s, bare text for 2 frames, cut at 11.85 s.
  - **Fills** animate inside the fixed box. Glitch, lava and line-art
    textures change on every frame (24 per second). The liquid gradient is
    stepped like stop-motion, holding each state 3–5 frames at 30 fps
    (about 7.5 updates per second) rather than flowing.
- **When they appear:**
  - the opening brand beat
  - date stamps
  - chapter cards
  - the occasional accent over b-roll (orange pixel block, `older`
    403.9 s)
  - the end card

## 5. Structure

**Cold open.** It starts mid-action on frame 0 at full volume: talking to
camera (`newest`), opening the blinds (`older`), packing a car
(`cards`, with `@JUSTINWU` over it). A brand beat lands by about 20 s:
wordmark, date stamp, first card, sign-off. In `cards` a spoken line
sets the scene, the music hits, and written narration says what the period
was like.

The fullest brand build is `older`, which runs 9.7–16 s:

1. A flat lime rectangle pops on (9.69 s).
2. Orange `BYJUSTINWU` pops on inside it (9.81 s).
3. Both glitch into pixel blocks, and the rectangle drops (10.36 s).
4. The type holds alone over the window for 1.6 s.
5. A scrambling manga-map rectangle pops in behind it (11.99 s).
6. The type blinks off one frame before the cut (12.75 s).
7. The serif title card (12.79–15.97 s).
8. The sign-off lands on the next cut
   (15.98 s) and stays over three shots, about 4.4 s.

The 2026 opens compress this to the wordmark alone (`cards`) or a
date stamp alone (`newest`).

**Chapters.** 4–7 per video, median 172 s (p10 75 s, p90 350 s). Names are lowercase days, places or events, with asides allowed. The words are not stored here.

**Chapter transitions:**

1. **Full card** (2026, `cards`). A pixel-mosaic wipe of 8–26 frames
   (0.3–1.1 s), then the title card. The card holds **137 frames (5.71 s)**
   in three of five cases (the others run 3.5 and 4.7 s), and its audio drops
   to silence. Then a hard cut into the chapter.
2. **Date stamp only** (`newest`). A stamp over the first shot of the
   new day (6 s, 361.5 s), with no card.
3. **Flat colour card** (2025). Serif title for 3.2 s (`older`
   12.79 s, over footage) to 7.6 s (795 s, on flat orange).

Use a card for a new city, job or phase, and a stamp for a new day in the
same thread. That split is my reading of the difference between the two 2026
videos. It is a Jev question at runtime.

**4:3 inserts.** The closing monologue of `newest` (850–910 s) runs
pillarboxed 4:3 inside the 16:9 frame. `older` is a 4:3 file
throughout.

**Outro.** A reflective monologue or recap, then an ~8 s brand beat:

- the manifesto title plus sign-off (`newest`)
- an end card plus the `@JUSTINWU` marquee, bookending the open
  (`cards`)
- a party montage colour-drifting into the orange card with a pixel logo
  while the music fades for 15.8 s (`older`)

There's no subscribe prompt in any of them.

## Who decides what at build time

- **Fixed by the profile** (100 values): shot lengths, fades, levels, card
  silence, type positions and holds, palette, layer motion.
- **Jev** (`conductor/jev.py`, choice/noul only; Jev picks and never writes):
  - `section_role` for each clip: talking, b-roll, burst or establishing
  - `chapter_break` (does a new day or place start here?)
  - `chapter_transition` (card or stamp)
  - `music_under_talk` (keep a ducked bed or play dry)
  - `cold_open_line`, chosen from up to 249 transcript lines filled in at
    build time
- **Taste model** (creative): chapter title wording, the written-narration lines,
  and building the rectangle textures and cards as code-generated graphics.
- **Code** (deterministic): timecodes, frame snapping, the ±100 ms onset
  snap, loudness targets, and mapping a clip's clock time to "noon" or
  "late evening".

## What the data could not answer

- **Exact fonts, sizes and tracking.** Frames only show a neutral grotesque
  (SF Pro-like) and a Times-like serif at 360–720 px. This needs his Final Cut
  or Motion titles.
- **2026 chapter-card and end-card motion.** The excerpts stop at 90 s and
  the 2026 cards start at 172 s. The intro layers and the 2025 card are
  measured; the mosaic wipe and the tiled `@JUSTINWU` end card are not. This
  needs excerpts around `cards` 172, 365, 463, 872, 1074 and 1134 s,
  or the project.
- **Tempo and beat grid.** The 400 ms loudness window smears beats. This
  needs stems or a full-rate onset pass.
- **Music choice** (genre, tracks). Not in the data.
- **Duck curve shape.** Measured at 0.5 s through a side-channel proxy, and
  camera audio is also stereo. This needs separate dialogue and music tracks.
- **J vs L.** Voice crosses 31% of talking cuts, but the data can't show
  which side leads.
- **His line timing beyond the first 90 s.** The auto-captions are not his
  burned-in lines. Hold times come from three lines in one excerpt.
- **Colour grade**, and whether the 4:3 inserts come from a camcorder or a
  crop.

## Reproduce

```bash
# unzip both bundle parts into one folder, then:
python styles/byjustinwu/study/analyze.py /path/to/bundle
# writes measurements.json into the gitignored study/data folder (deterministic)
```

The script's docstring defines the window and section classes (speech from
caption words, montage as four or more consecutive non-talking shots with a
median of 2.5 s or less, intro and outro as the first and last 30 s).

```bash
# the three <video_id>_first90s_480p*.mp4 excerpts in one folder (needs ffmpeg):
python styles/byjustinwu/study/motion.py /path/to/excerpts
# writes motion.json into the gitignored study/data folder (deterministic)
```

`motion.py` measures texture update rates and pop events inside fixed
regions. Entry, exit and stagger times in this guide were read by stepping
frame by frame through the same excerpts.

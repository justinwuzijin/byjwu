# Style profiles

A style profile holds one creator's editorial taste as data: pacing, music behaviour, typography, text treatments, and the background layer. The assembly engine reads it, and the iterate loop measures cuts against it. None of it is hardcoded, so swapping in the style study means editing a JSON file, not changing code.

```text
styles/
  base/profile.json          neutral defaults: every key the engine reads
  byjustinwu/profile.json    measured profile (extends base)
```

Load one with `--style byjustinwu` or `--style path/to/profile.json`. Without a flag, you get `byjustinwu`. `JEVID_STYLES_DIR` adds more folders to search (`os.pathsep`-separated). `profile.yaml` works too when PyYAML is installed.

## Envelope

| key | meaning |
|---|---|
| `schema` | always `"jevid.style"` |
| `schema_version` | `1`. A newer major version is refused, not half-read. |
| `name`, `version` | shown in reports and on markers |
| `extends` | parent profile name. Dicts merge key by key; lists and scalars replace. |
| `provisional` | `true` until the numbers come from a study. Reports and `assembly.md` flag it. |
| `description`, `provenance` | where the numbers came from (`study` points at the study once it exists) |
| `assumptions` | plain-language list of what the numbers assume. Shown in the report. |

Unknown top-level keys are kept and reported as warnings, so a study can add fields before the engine reads them. A bad value (a colour that is not `#RRGGBB`, a treatment name that does not exist, `cut_on_beat: "sometimes"`) is an error, with every problem listed.

## Sections

Section kinds are `intro`, `title_card`, `talking`, `montage`, `outro`, and `end_card`. `structure.order` lays them out and may repeat a kind (two `talking` blocks around a `montage`).

### `format`

`width`, `height`, `frame_rate`, `follow_footage`. With `follow_footage: true`, the sequence takes the first clip's size and rate. With `false`, the size comes from the profile and the rate still follows the footage.

### `structure`

| key | unit | meaning |
|---|---|---|
| `target_seconds` | s | length when neither `--target-seconds` nor the brief ("an 8-minute video") says |
| `tolerance_seconds` | s | the duration target's window |
| `order` | list | section sequence |
| `sections.<kind>.share` / `min_seconds` / `max_seconds` | fraction, s | budget for intro, montage and outro. Talking takes the rest. |
| `title_card` / `end_card` | `seconds`, `bars`, `text` | card length. `bars` wins when a beat grid is known, and the card ends on a downbeat. |
| `broll_chunk_seconds` | s | b-roll is cut into chunks of this length before judging |
| `speech_merge_gap_seconds` | s | cues closer than this merge into one range |
| `speech_handle_seconds` | s | padding kept around speech |
| `min_speech_seconds`, `keep_threshold` | s, 0–1 | ranges below the threshold are dropped |
| `intro_flash_share` | 0–1 | share of the intro that is fast b-roll flashes before the hook line |

### `pacing.<kind>` (intro, talking, montage, outro)

| key | meaning |
|---|---|
| `asl_seconds` | target average *visible* shot length: spine cuts plus cutaway edges. This is what iterate measures. |
| `shot_length` | `{median, p10, p90}` of a log-normal distribution. Montage and intro shot lengths are drawn from it and scaled so the mean matches `asl_seconds`. |
| `cut_on_beat` | `off`, `prefer` (move a cut at most `cuts.beat_snap_tolerance_seconds`, never into a word), or `always` (the beat nearest the drawn length) |
| `broll_cover` | share of talking time to cover with cutaways. Capped so the ASL target still holds. |

`pacing.tolerance` is the relative ASL window (0.25 means ±25%).

### `cuts`

`beat_snap_tolerance_seconds`, `min_shot_seconds`, `j_cut {probability, lead_seconds}`, `l_cut {probability, tail_seconds}` (written as `audioStart`/`audioDuration` on the clip), `punch_in {enabled, scale, every}` (alternating scale-ups on jump cuts), `cutaway {min_seconds, max_seconds}`, `broll_nat_sound_db`, `dissolve {sections, duration_seconds}`. `sections` lists the outgoing section kinds that get an FCPXML cross dissolve of `duration_seconds` at the cut. An empty list writes no transitions.

`cuts.broll` is the keyword-slot density: `min_seconds` / `max_seconds` (hard-clamped to 0.5–8), `target_seconds` (default 3, inside 2–4), `lead_seconds` (1–2, the uncovered head of a new on-camera shot), `punchlines` (lines that stay face-to-camera), and `coverage_level` (`low` / `moderate` / `high` map to 14 s / 9 s / 5 s spacing and a 30 s / 20 s / 12 s max hold). `moderate` is the 9 s median spacing. `spacing_seconds` and `max_hold_seconds` apply when `coverage_level` is omitted.

### `music`

| key | meaning |
|---|---|
| `role` | audio role for the bed (`music`) |
| `bed_db`, `floor_db` | level with no dialogue; the silence value fades start and end at (-96) |
| `fade_in` / `fade_out` | `{seconds, curve}`, curve ∈ `linear`, `easeIn`, `easeOut`, `easeInOut` |
| `duck` | `{enabled, depth_db, attack_seconds, release_seconds, merge_gap_seconds, curve}`. Dialogue spans closer than `merge_gap_seconds` duck as one. |
| `song_change` | `{transition: crossfade / cut / fade_through, crossfade_seconds, at: section_boundary}` |
| `sections.<kind>` | `{bed_db, duck}` per section, e.g. louder and unducked in the montage |
| `beats` | `{detect, min_bpm, max_bpm, fallback_bpm, beats_per_bar}` |
| `start_on_downbeat` | start each song at its first downbeat |

Everything is written as `adjust-volume` keyframes on the music clip. There are no fade handles to reinterpret.

### `typography`

`family` (e.g. `SF Pro`, which the font-compliance metric checks), then `title` and `subtitle`:

| key | unit | meaning |
|---|---|---|
| `font`, `face` | name | e.g. `SF Pro Display` / `Heavy`, `SF Pro Text` / `Semibold` |
| `size` | fraction of frame height | 0.042 on 1080p is a 45 pt Basic Title |
| `position` | fractions of frame, centre origin, +y up | `[0, -0.33]` is the lower third |
| `case` | `upper`, `lower`, `sentence`, `title`, `as_is` | |
| `tracking` | em | written as `kerning` = tracking × size |
| `color`, `stroke {color, width}`, `shadow {color, distance, angle, blur}` | hex, px | |
| `lane` | int > 0 | connected-clip lane |
| `treatments` (title) | names | rotated or chosen per card |
| subtitle `max_chars_per_line`, `max_lines`, `min_seconds`, `max_seconds` | | card rules |
| subtitle `emphasis {rate, treatments}` | 0–1, names | about `rate` of lines get a distortion, chosen per line |

Titles are Final Cut's Basic Title with the text style set on each clip, so the font, size, colour, and tracking survive import.

### `text_treatments`

Named distortions. Every value is data:

| key | value |
|---|---|
| `scale` | `[[t, [sx, sy]], …]` keyframes |
| `position` | `[[t, [x, y]], …]`, fractions of frame |
| `rotation` | `[[t, degrees], …]` |
| `corners` | `{topLeft, topRight, botLeft, botRight: [dx, dy]}`, fractions of frame (corner pin / skew) |
| `tracking` | em, overrides the style's tracking |

`t` is seconds from the title's start, or from its end when negative. `none` must exist.

### `background`

The abstract rectangle layer.

| key | meaning |
|---|---|
| `enabled`, `placement` (`under` / `over`), `lane` | `under` sits below the storyline; the A-roll is inset over it |
| `sections.<kind>` | where the layer appears |
| `aroll_inset.<kind>` | A-roll scale in those sections (1.0 hides the layer) |
| `change {every, max_seconds}` | a new plate per section, split when longer than this |
| `render_scale` | PNG resolution relative to the frame |
| `layout` | `mode` (`grid`, `random`, `mixed`), `grid {columns, rows, merge_probability, fill}`, `random {count}`, `size_ratios`, `size_range`, `gutter`, `margin` |
| `contortion` | `stretch`, `shear_degrees`, `slices`, `slice_shift`, `smear` (pixel sort), `mirror` |
| `motion` | `drift`, `pulse`, `pulse_on_beat`, `floating` (separate drifting rectangles), `floating_size`, `rotation_degrees` |
| `opacity`, `base_color`, `palette` | |
| `art {folder, procedural, seed}` | source images (PNG, or any format with Pillow), else procedural art (`interference`, `noise`, `stripes`) in the palette |

Plates and floaters are PNG stills under `<out>/assets/background/`, placed as connected `video` clips with transform keyframes. The same seed and profile give the same bytes.

### `metrics`

What iterate checks after each round: `subtitle_coverage_min`, `on_beat_min` (share of cuts on a beat in `always` sections), `fade_tolerance_seconds`, `duck_tolerance_db`, `background_coverage_min`, and `font_compliance_min`. The ASL and duration targets come from `pacing` and `structure`.

## Updating a measured profile

`styles/byjustinwu/profile.json` is measured (`provisional: false`). A later study should:

1. Keep the keys. Replace numbers in the profile with the new measurements.
2. Point `provenance.study` at the study. Evidence notes stay aggregate stats, with no video ids.
3. Rewrite `assumptions` as what was measured.
4. New measurements the engine does not read yet can go in as new keys. They load with a warning until code uses them.
5. Run `python -m pytest tests/test_style_profile.py tests/test_assembly_engine.py tests/test_byjustinwu_profile.py`.

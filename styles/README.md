# Style profiles

A style profile is the measured editing grammar of one creator, stored as data
the assembly engine reads when it turns raw clips and music into an FCPXML.
Each style lives in `styles/<name>/`:

| file | what it is |
|---|---|
| `profile.json` | machine-readable parameters (format below) |
| `STYLE.md` | the same style written for an editor, with evidence and open questions |
| `study/` | the script that recomputes every measured number, and its output |

The taste log and room notes refer to a profile as `styles/<name>/profile`.
`conductor.style.load_profile` accepts that, a bare name (`byjustinwu`), or a
path, and validates the file before returning it.

```python
from conductor.style import load_profile

style = load_profile("byjustinwu")
style.value("cut_rhythm.broll.shot_median_s")  # 3.5
style.param("music.card_silence_s")            # value, unit, range, confidence, evidence
style.jev_questions()                          # question specs for conductor/jev.py
```

## Format: `jevid.style-profile/1`

No other agent had defined a `styles/<name>/profile` format when this was
written (checked open PRs and remote branches), so this is the first version.

```jsonc
{
  "schema": "jevid.style-profile/1",
  "name": "byjustinwu",
  "title": "...", "summary": "...", "status": "measured",
  "study": {"script": "...", "measurements": "...", "evidence_key": "..."},
  "sources": [
    {"id": "<youtube id>", "duration_s": 910.1, "fps": 30, "weight": 0.35, "role": "..."}
  ],
  "params": {
    "<group>": {
      "<param>": {
        "value": 3.5,             // any JSON value
        "unit": "s",              // optional
        "range": [3.3, 4.0],      // optional [low, high]; numeric values must sit inside
        "confidence": 0.75,       // required, 0..1
        "evidence": "stat: ...",  // required, video id + timestamps or a measurements key
        "decided_by": "profile"   // optional: profile (default) | jev | taste_model
      },
      "<subgroup>": { "...": "groups nest freely" }
    }
  },
  "jev_questions": [
    {"key": "section_role", "primitive": "choice", "instructions": "...",
     "options": {"talking": "when it applies", "...": "..."},
     "add_none": false, "feeds": "cut_rhythm.broll.shot_median_s"},
    {"key": "cold_open_line", "primitive": "choice", "instructions": "...",
     "options_from": "transcript_lines"},
    {"key": "chapter_break", "primitive": "noul", "instructions": "...",
     "true": "...", "false": "..."}
  ],
  "unknowns": [
    {"topic": "exact fonts", "why": "...", "needs": "..."}
  ]
}
```

Rules the loader enforces:

- Any object with a `value` key under `params` is a parameter. It needs
  `confidence` in [0, 1] and non-empty `evidence`. Every other object there is
  a group and must not be empty.
- `decided_by` says who settles the value when a project is built:
  - `profile`: use the value as is.
  - `jev`: the value is the prior. The runtime call is the matching
    `jev_questions` entry, asked through `conductor/jev.py`. Jev only picks
    from the options given and never writes.
  - `taste_model`: a creative call such as title wording or generated rectangle
    textures, settled by the taste model. The value describes what to aim for.
- `jev_questions` use `choice` or `noul`, the two `cutmcp.jev` constructors
  conductor shares. A choice has fixed `options`, or `options_from` naming a
  source the engine fills at build time. Either way it is capped at
  `cutmcp.jev.MAX_OPTIONS` (250, counting the added `none`). `feeds`, if set,
  must name an existing parameter.
- `unknowns` lists what the study could not answer. Each entry needs a
  `topic` and a `why`.

Deterministic rules stay in the engine: timecodes, frame snapping, loudness
maths, and mapping a clip's clock time to a word like "late evening". The
profile only carries the numbers those rules use.

# Style profiles

A style profile is the measured editing grammar of one look, written so an
assembly engine can build a timeline from it. One folder per style:

```
styles/<name>/
  profile.json      machine-readable parameters (schema below)
  STYLE.md          the same style for an editor to read
  study/            the analysis that produced the numbers
    analyze.py      recomputes every measured value from the source bundle
    sections.json   the hand labels analyze.py needs (the only interpretive input)
    measured.json   analyze.py output, committed so profile values can be checked
  reference/        a few small frame crops, optional
```

`conductor/style.py` loads and validates profiles:

```python
from conductor.style import load_profile

style = load_profile("byjustinwu")          # or a path to a profile.json
style.value("rhythm.montage.shot_median_s") # 2.752
style.param("mix.talking_lufs")             # the whole leaf: value, confidence, evidence...
style.below(0.5)                            # params to treat as soft defaults
style.taste_prefs()                         # conductor_taste as Cut Conductor taste prefs
style.questions("p_")                       # runtime questions for one batched conductor.jev.ask
style.prior("cold_open_kind")               # the prior as an Answer, for dry runs
```

`JEVID_STYLES_DIR` overrides where names are looked up. Loading raises
`ConductorError` listing every schema problem.

## Schema `jevid.style_profile/1`

Top level:

| key | type | meaning |
|---|---|---|
| `schema` | `"jevid.style_profile/1"` | required |
| `style` | string | style name, matches the folder |
| `version` | string | bump when values change |
| `summary` | string | one paragraph, optional |
| `sources` | list | `{video_id, title, duration_s, upload_date, weight, note}`; `weight` in [0, 1] says how much a source counted |
| `evidence_conventions` | string | how to read evidence notes, optional |
| `params` | object | tree of groups and parameters |
| `runtime_questions` | list | judgement calls routed to Jev at runtime |

### Params

`params` nests groups (objects without a `value` key) down to parameters
(objects with one). Keys are `lower_snake_case`. A key starting with `_` is a
string note and is ignored. A bare number or string is an error: every value
carries its evidence.

| param key | required | meaning |
|---|---|---|
| `value` | yes | any JSON value the engine consumes |
| `confidence` | yes | 0 to 1. See the style's `evidence_conventions` |
| `evidence` | yes | one line: `video_id@seconds`, or a stat and its sample size |
| `unit` | no | `s`, `LUFS momentary`, `dB`, `cuts/min`, ... Positions and sizes without a unit are fractions of frame width/height, origin top-left |
| `measured` | no | dotted key into `study/measured.json` (list indexes allowed: `ducking.beds.1.depth_db`) |
| `adjusted` | no | why `value` deliberately differs from `measured`. Without it, a numeric value must be within 5% of the measurement |
| `decided_by` | no | `profile` (default: use `value`), `jev` (ask `question`; `value` is the modal answer), `editor` (words only a person supplies) |
| `question` | with `jev` | id of a `runtime_questions` entry |
| `note` | no | anything an engine author should know |

### Runtime questions

Anything the engine must decide per project, section, clip, or line that is
a judgement rather than arithmetic goes to Jev through `conductor.jev.ask`.
The profile only describes the question:

| key | meaning |
|---|---|
| `id` | `lower_snake_case`, unique |
| `kind` | `choice` or `noul` (the two kinds Cut Conductor's Jev client parses) |
| `applies_to` | what one question is asked about: `project`, `clip_run`, `talking_section`, `transcript_line`, ... |
| `instructions` | neutral wording; editorial preference belongs in `prior`, not here |
| `options` | `choice` only: key to description. Same cap as `cutmcp.jev.MAX_OPTIONS` |
| `add_none` | `choice` only, default true, as in `cutmcp.jev.choice` |
| `true` / `false` | `noul` only, optional verdict wording |
| `prior` | `choice`: option key to probability, sums to 1. `noul`: a probability |
| `sets` | the param or group the answer feeds |

Jev selects; it never writes. Titles, taglines, narration lines, and asides
are `decided_by: editor`. Dates and times in stamps come from clip metadata,
which is arithmetic, so they are `decided_by: profile`.

### Checks

`tests/test_conductor_style.py` validates every committed profile, checks
each `measured` pointer against `study/measured.json`, and checks that the
study folder carries no media beyond small reference crops. Re-run the study
after changing labels:

```bash
python styles/byjustinwu/study/analyze.py --bundle /path/to/bundle --out styles/byjustinwu/study/measured.json
python -m pytest tests/test_conductor_style.py
```

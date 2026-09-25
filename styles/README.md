# Style profiles

A style profile is one creator's editorial taste as data. The assembly engine
reads it when it turns raw clips and music into an FCPXML. Each style lives
in `styles/<name>/`:

| file | what it is |
|---|---|
| `profile.json` | parameters in the `jevid.style` schema (v1). `extends` names a parent; dicts merge key by key |
| `STYLE.md` | the same style written for an editor, with aggregate evidence |
| `study/` | the script that recomputes measured numbers, and its output |

`styles/base/profile.json` lists every key the engine reads. A creator profile
only states what differs. The full key list is in `docs/style-profile.md`.

Load one by bare name, by the `styles/<name>/profile` form, or by path.
`JEVID_STYLES_DIR` adds more folders to search.

```python
from conductor.style import load_style

style = load_style("byjustinwu")
style.get("pacing.montage.asl_seconds")
style.get("music.fade_in.seconds")
style.summary()  # numbers a decision batch sees
```

Unknown top-level keys are kept and reported as warnings, so a study can add
fields before the engine reads them. A bad value is an error.

Video ids, frame crops, and excerpt files stay in the gitignored
`styles/<name>/study/data/`, `styles/<name>/reference/`, and
`styles/<name>/ref/` folders. `study/analyze.py` and `study/motion.py` read
ids only from `study/data/sources.json`.

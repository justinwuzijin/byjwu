# Credits

Retake detection and word-boundary hygiene are clean-room reimplementations
from public descriptions. No code was copied.

- **Descript** — Remove Retakes (mark earlier versions) and Shorten Word Gaps
  (shorten a gap to a target instead of deleting it).
- **Gling** — bad-take removal from a transcript.
- **Selects** — group the takes of a line and keep the best.
- **ButterCut** — restatement trimming and keeping a sentence whole. Ideas
  only; the project is not under an MIT, BSD, ISC, or Unlicense licence.
- **auto-editor** (Unlicense) — asymmetric margin, minimum cut and minimum
  clip, and a dissolve only where the removed source is long enough. The
  behaviour was reimplemented; the auto-editor source was not copied.
- **Mosaic** — silence and filler as a counter-example (do not cut every
  inter-word gap; ambiguous fillers stay proposals).

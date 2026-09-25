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

Ideas reimplemented in this repo. No code was copied from the projects below.
The licence rule is in the research note: only MIT, BSD, ISC, or Unlicense
code may be adapted, and it must be credited. Everything else is ideas only.

## Timeline linter, critic, and eval harness

`conductor/plan.py`, `conductor/lint.py`, `conductor/critic.py`, and
`eval/timeline_lint.py` are a clean-room reimplementation of public
descriptions:

- **Cardboard** (usecardboard.com), hard problem 09, "Verification: there's
  no linter for video." Technical checks first, then pacing, story, and
  brand fit. No source published.
- **CutClaw** (GVCLab/CutClaw, arXiv 2603.29664). The reviewer gate: identity,
  non-overlap, and a reject that backtracks. The repository has no licence,
  so it was not read and no code was copied.
- **EditDuet** (SIGGRAPH 2025, arXiv 2509.10761). An editor/critic pair where
  the critic can only give feedback or accept the cut. It cannot add a shot.
- **VlogReward** (arXiv 2607.22632). A 1–5 rubric read from the JSON edit
  plan rather than a render, and tampered plans as regression tests.
- **ButterCut** (barefootford/buttercut). PolyForm Noncommercial. Ideas only:
  check that media is reachable, and flag a point made twice. No code copied.
- **Diffusion Studio** (`core` and `editor` are MPL-2.0). See the audit below.

The critic is a taste call (`timeline_critic`) on the router. The default
taste model is `grok-4.7-medium`. A Claude id selects Opus.

## Diffusion Studio audit

Searched the tree for Diffusion Studio names (`diffusion`, `DiffusionStudio`,
MPL notices, and `core`/`editor` package markers). **No file contains copied
or closely translated code from Diffusion Studio.** Nothing in `conductor/`
or `cutmcp/` carries an MPL-2.0 obligation. The idea credited here is the
general one that a timeline can be described and checked as structured data
before it is rendered. No Diffusion Studio source was copied.

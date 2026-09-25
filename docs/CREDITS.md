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
  inter-word gap; ambiguous fillers stay proposals). Montage BPM bands
  (shorter shots in higher-energy sections) informed the keypoint montage
  durations. Ideas only.

Music-structure cut snapping (`conductor/assembly/snap.py`) is a clean-room
reimplementation of public descriptions. No code was copied.

- **CutClaw** (GVCLab/CutClaw) — keypoints and an AV-harmony check (visual
  cuts near musical events). The repository has no licence, so it was not
  read and no code was copied.
- **Cardboard** — beat sync from percussion / onset energy.
- **Mosaic** — montage shot length follows the local BPM band and energy.

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

## Transcript-keyword B-roll

`conductor/assembly/broll.py` slots cutaways from transcript keywords and
matches them with caption text. Ideas only; no code was copied.

- **B-Script** — keyword anchors, a 0.5–8 s cutaway, about 9 s between
  cutaways, and a cap on how long A-roll holds with no cutaway.
- **LAVE** — a short visual narration (title and summary) per B-roll clip,
  embedded as text and ranked against the words around the slot.
- **EditDuet** — the taste model may only veto. Here it vetoes a slot or
  picks among the top three descriptions. It cannot add a slot or a time.
- **Mosaic** — `coverage_level` presets (low, moderate, high) that change
  how often a cutaway is allowed.
- **Descript** — sample fewer frames on a static shot and more when the
  frame is full of text. The mock backend does not decode frames.

## Transcript segment index

`conductor/segments.py` caches a transcript segment index and asks Jev once
to pick from it. Ideas only; no code was copied.

- **Clip Fast** (Burhan Usman, 2026) — pre-split a long transcript into
  segments under a length cap, then one Jev call picks the matches.
- **TextTiling** (Hearst, 1997) — topic boundaries where lexical cohesion
  between neighbouring windows drops. The scorer here is written from that
  description.
- **ClipsAI** — the same windowed cohesion idea applied to sentences. The
  repository is MIT; this module does not copy it.

## Diffusion Studio audit

Searched the tree for Diffusion Studio names (`diffusion`, `DiffusionStudio`,
MPL notices, and `core`/`editor` package markers). **No file contains copied
or closely translated code from Diffusion Studio.** Nothing in `conductor/`
or `cutmcp/` carries an MPL-2.0 obligation. The idea credited here is the
general one that a timeline can be described and checked as structured data
before it is rendered. No Diffusion Studio source was copied.

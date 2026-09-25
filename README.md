# byjwu

byjwu helps me edit [@byjustinwu](https://www.youtube.com/@byjustinwu) videos: raw footage and music go in, and a Final Cut Pro FCPXML comes out.

## How it works

```text
footage, music, or an FCPXML
        ↓
Grok Bot room  →  engine (conductor)  ←  Jev + Claude Opus 5.5
        ↓
FCPXML  →  Final Cut Pro
```

- **In:** footage, music, or an FCPXML, dropped in the Grok Bot room or `~/Desktop/byjwu-in`.
- **Room:** the Grok bots coordinate: Cut Conductor, Pacing, Style, Type & Subs, and Colour.
- **Decisions:** Jev makes the logical calls. Claude Opus 5.5 makes the taste calls.
- **Engine:** `conductor` builds the edit, then marks it or applies the safe cuts.
- **Out:** an FCPXML in `~/Desktop/byjwu-out`, opened in Final Cut.

## What changes in your FCPXML

Each drop gets a new folder, `~/Desktop/byjwu-out/<name>-<time>/`. Open the file named in `room.md`. A folder of clips also gets `starter.fcpxml` (filename order) before the rounds. Your drop, your media, and your library stay as they were. Import makes a new event.

Each round is `vN/`:

- `timeline.fcpxml` is the timeline that round started from.
- `timeline.conductor.fcpxml` is the marked copy. Clips, in and out points, offsets, connected clips, roles, effects, and markers you already had stay. New markers are the only addition. The file is rewritten, so it is not a byte-for-byte copy of the export.
- `timeline.conductor.applied.fcpxml` appears only when a cut is applied. That file is the cut, with a one-frame `CC cut` marker at each join.
- `timeline.conductor.md` and `.json` are the round report. `room.md` and `room.json` summarize the run. `iterate.json` records why it stopped.

A marker sits on the spine clip that owns the region, at that moment in the clip's source time, one frame long. The name is `CC tighten`, `CC remove`, `CC review`, or `CC escalate`, plus a short label and the timecode. The note starts `Cut Conductor shadow proposal. No edit was applied.` and then lists the action, confidence, risk, and why. Review and escalate are to-do markers (`completed="0"`). A cut the gate would apply is a standard marker. Keeps leave no marker. There are no chapter markers. Colour lives in the note as `color=`.

Logic-first is the default (`CONDUCTOR_DECISION_MODE=logic-first`). A measured rule cuts without a model score: a bare uncovered gap of at least 1.25s, dead air past 0.5s when audio or word timings exist, a flash under 5 frames, an exact duplicate source range, or a silent hold past 45s (or the style profile's max shot). Confidence is the margin past that threshold, starting at 0.80. The model sees the proposed cut and may only veto; a veto stays a review marker on the marked copy and is not cut. `model-gated` restores the old gate: confidence at least 0.80, risk at most 0.35, and the pass is mechanical. Dialogue, pacing, and colour are marked and left in place. A Jev fallback with no measured rule is marked, never cut.

On the applied file the range is lifted from the primary storyline. A whole gap or clip goes away. A tighten keeps the head (4s unless taste says otherwise) and lifts the tail. A range inside a clip splits it: new in point, out point, and a rippled offset. Spine clips after the cut move earlier, and the sequence duration shrinks by the time removed. Connected B-roll and secondary storylines keep their own offsets. A stretch they cover stays. A connected clip that would be cut in half is removed, and the report says so. A spine that contains a transition is left uncut. The proposal marker stays on the marked copy. Effects, roles, and keywords on a kept piece stay. Audio levels are not rewritten.

Each applied cut leaves a one-frame marker on the clip that now sits at the cut. The name is `CC cut`, what was removed, its duration, and the timeline timecode it came from. The note carries the rule and the confidence.

Graphics stay off unless the room turns them on. When on, they are added on a free connected lane of the file you open. Subtitles, and titles with no distortion or a scale move, are Basic Title clips. Glitch, RGB split, wave, and blur-in titles are transparent movies in a folder named `<that file>.assets` beside the XML. Rectangles are Shapes generator clips, with position and scale keyframes, a blur, and a hue shift. Nothing already on the timeline is retimed. Grades are never written.

Details are in [docs/](docs/technical.md).

built at a grok bot design build night in los angeles 09/22/26

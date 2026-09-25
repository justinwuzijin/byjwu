"""Word-boundary cut hygiene, after rules and vetoes and before apply.

Ideas from auto-editor (Unlicense: margin, smooth, and the transition
minimum), Descript Shorten Word Gaps, ButterCut's review pass, and Mosaic's
silence notes. Reimplemented from public descriptions; no code copied.

Dead air from 0.5s up to 1.25s is shortened to the style-profile target.
Longer bare gaps stay removals. Handles, minimum cut, minimum clip, word
and frame snapping, sentence integrity, and dissolves are all deterministic.
"""

from __future__ import annotations

from fractions import Fraction

from .style import StyleProfile

_SENTENCE_END = frozenset(".!?")


def hygienize(
    cuts: list[dict],
    words: list | None = None,
    *,
    profile: StyleProfile | None = None,
    frame_duration: Fraction = Fraction(1, 24),
    timeline_end: Fraction | None = None,
) -> list[dict]:
    """Return the cuts that should be applied, frame-snapped and non-overlapping."""
    profile = profile or StyleProfile()
    words = list(words or [])
    prepared = [_copy(cut) for cut in cuts if cut.get("end", 0) > cut.get("start", 0)]
    prepared = [_shorten_gap(cut, profile) for cut in prepared]
    prepared = [cut for cut in prepared if cut["end"] > cut["start"]]
    prepared = _apply_margins(prepared, words, profile)
    prepared = [cut for cut in prepared if cut["end"] - cut["start"] >= profile.min_cut]
    prepared = _absorb_islands(prepared, profile, timeline_end)
    prepared = [_snap_words(cut, words) for cut in prepared]
    prepared = [_keep_sentences(cut, words) for cut in prepared]
    prepared = [cut for cut in prepared if cut["end"] > cut["start"]]
    prepared = [_snap_frame(cut, frame_duration) for cut in prepared]
    prepared = [cut for cut in prepared if cut["end"] > cut["start"]]
    prepared = _resolve_overlaps(prepared)
    for cut in prepared:
        removed = cut["end"] - cut["start"]
        if profile.allow_dissolves and removed >= profile.dissolve_min_removed:
            cut["transition"] = "dissolve"
        else:
            cut["transition"] = None
    return prepared


def margins_for(previous_end: Fraction, next_start: Fraction, profile: StyleProfile | None = None) -> tuple[Fraction, Fraction]:
    """Post-roll and pre-roll that fit in the gap without crossing a word."""
    profile = profile or StyleProfile()
    gap = next_start - previous_end
    if gap <= 0:
        return Fraction(0), Fraction(0)
    want = profile.post_roll + profile.pre_roll
    if want <= gap:
        return profile.post_roll, profile.pre_roll
    scale = gap / want
    return profile.post_roll * scale, profile.pre_roll * scale


def _shorten_gap(cut: dict, profile: StyleProfile) -> dict:
    kind = cut.get("kind") or cut.get("reason")
    if kind not in {"dead_air", "silence_gap", "silence"}:
        return cut
    start = Fraction(cut["start"])
    end = Fraction(cut["end"])
    duration = end - start
    if duration < profile.dead_air_shorten_min or duration >= profile.dead_air_remove_min:
        return cut
    target = profile.gap_target if profile.gap_target < duration else duration
    kept_tail = end - target
    updated = _copy(cut)
    updated["start"] = start
    updated["end"] = kept_tail
    updated["kind"] = "shorten_gap"
    return updated


def _apply_margins(cuts: list[dict], words: list, profile: StyleProfile) -> list[dict]:
    if not words:
        return cuts
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    adjusted = []
    for cut in cuts:
        if cut.get("kind") in {"shorten_gap", "dead_air", "silence_gap", "silence"}:
            adjusted.append(cut)
            continue
        previous = [word for word in ordered if word.end <= cut["start"]]
        following = [word for word in ordered if word.start >= cut["end"]]
        start = Fraction(cut["start"])
        end = Fraction(cut["end"])
        if previous and following:
            post, pre = margins_for(previous[-1].end, following[0].start, profile)
            margin_start = previous[-1].end + post
            margin_end = following[0].start - pre
            start = max(start, margin_start)
            end = min(end, margin_end)
        elif previous:
            start = max(start, previous[-1].end + profile.post_roll)
        elif following:
            end = min(end, following[0].start - profile.pre_roll)
        if end <= start:
            continue
        updated = _copy(cut)
        updated["start"] = start
        updated["end"] = end
        adjusted.append(updated)
    return adjusted


def _absorb_islands(cuts: list[dict], profile: StyleProfile, timeline_end: Fraction | None) -> list[dict]:
    ordered = sorted(cuts, key=lambda cut: (cut["start"], cut["end"]))
    if not ordered:
        return []
    merged = [ordered[0]]
    for cut in ordered[1:]:
        previous = merged[-1]
        island = cut["start"] - previous["end"]
        if Fraction(0) <= island < profile.min_clip:
            previous["end"] = max(previous["end"], cut["end"])
            if cut.get("explicit_trim"):
                previous["explicit_trim"] = True
            continue
        merged.append(cut)
    if timeline_end is not None and merged:
        # A kept island at the head is not between cuts. Islands are interior.
        return merged
    return merged


def snap_span(start, end, words) -> tuple:
    """Pull a range out to the word edges it overlaps. Same rule as a cut snap."""
    snapped = _snap_words({"start": start, "end": end}, words)
    return snapped["start"], snapped["end"]


def _snap_words(cut: dict, words: list) -> dict:
    if not words or cut.get("explicit_trim"):
        snapped = _copy(cut)
    else:
        snapped = _copy(cut)
    start = Fraction(cut["start"])
    end = Fraction(cut["end"])
    for word in words:
        if word.start < end < word.end:
            end = word.end
        if word.start < start < word.end:
            start = word.start
    if end <= start:
        return cut
    snapped = _copy(cut)
    snapped["start"] = start
    snapped["end"] = end
    return snapped


def _keep_sentences(cut: dict, words: list) -> dict:
    if not words or cut.get("explicit_trim") or cut.get("kind") in {"retake", "restatement", "shorten_gap"}:
        return cut
    sentences = _sentences(words)
    start = Fraction(cut["start"])
    end = Fraction(cut["end"])
    for sentence_start, sentence_end, _words in sentences:
        if sentence_start < start < sentence_end:
            start = sentence_end
    if end <= start:
        cut = _copy(cut)
        cut["start"] = start
        cut["end"] = start
        return cut
    updated = _copy(cut)
    updated["start"] = start
    updated["end"] = end
    return updated


def _snap_frame(cut: dict, frame: Fraction) -> dict:
    updated = _copy(cut)
    updated["start"] = _quantize(Fraction(cut["start"]), frame)
    updated["end"] = _quantize(Fraction(cut["end"]), frame)
    if updated["end"] < updated["start"]:
        updated["end"] = updated["start"]
    return updated


def _quantize(moment: Fraction, frame: Fraction) -> Fraction:
    if frame <= 0:
        return moment
    steps = int(round(moment / frame))
    return steps * frame


def _sentences(words: list) -> list[tuple[Fraction, Fraction, list]]:
    ordered = sorted(words, key=lambda word: (word.start, word.end))
    groups: list[list] = []
    bucket: list = []
    for word in ordered:
        bucket.append(word)
        text = str(word.text).rstrip()
        if text and text[-1] in _SENTENCE_END:
            groups.append(bucket)
            bucket = []
    if bucket:
        groups.append(bucket)
    return [(group[0].start, group[-1].end, group) for group in groups if group]


def _resolve_overlaps(cuts: list[dict]) -> list[dict]:
    ordered = sorted(cuts, key=lambda cut: (Fraction(cut["start"]), Fraction(cut["end"])))
    kept: list[dict] = []
    for cut in ordered:
        if kept and cut["start"] < kept[-1]["end"]:
            if cut["end"] <= kept[-1]["end"]:
                continue
            cut = _copy(cut)
            cut["start"] = kept[-1]["end"]
            if cut["end"] <= cut["start"]:
                continue
        kept.append(cut)
    return kept


def _copy(cut: dict) -> dict:
    return dict(cut)

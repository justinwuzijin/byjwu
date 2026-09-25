"""Plain-language notes for the markdown report.

The tables stay the contract. This is the paragraph an editor reads first:
what the timeline is doing, which stretches are empty primary, and which
signals were left as markers. Nothing here decides a cut.
"""

from __future__ import annotations

from fractions import Fraction

from .candidates import Candidate
from .decide import Proposal
from .fcpxml import Sequence
from .metrics import section_pacing
from .timeutil import rate_label, short_clock


def editor_notes(
    *,
    sequences: list[Sequence],
    candidates: list[Candidate],
    proposals: list[Proposal],
    transcript_present: bool,
) -> list[str]:
    if not sequences:
        return []
    by_id = {item.candidate_id: item for item in proposals}
    paragraphs = [_identity(sequences, transcript_present)]
    pace = _pace(sequences, candidates)
    if pace:
        paragraphs.append(pace)
    mechanical = _mechanical(sequences, candidates, by_id)
    if mechanical:
        paragraphs.append(mechanical)
    holds = _holds(candidates)
    if holds:
        paragraphs.append(holds)
    stringout = _stringout(candidates)
    if stringout:
        paragraphs.append(stringout)
    cards = _cards(candidates)
    if cards:
        paragraphs.append(cards)
    music = _music(candidates)
    if music:
        paragraphs.append(music)
    reuse = _reuse(sequences, candidates)
    if reuse:
        paragraphs.append(reuse)
    picture = _picture(sequences, candidates)
    if picture:
        paragraphs.append(picture)
    return paragraphs


def _identity(sequences: list[Sequence], transcript_present: bool) -> str:
    sequence = sequences[0]
    duration = sequence.duration
    if duration is None:
        duration = sum((clip.duration for clip in sequence.spine), Fraction(0))
    rate = rate_label(sequence.frame_duration)
    if sequence.width and sequence.height:
        size = f"{sequence.width}×{sequence.height}"
    else:
        size = "an unknown frame size"
    name = sequence.name
    if len(sequences) > 1:
        name = f"{name} and {len(sequences) - 1} more"
    sentence = f"{name} runs {_span(duration)} at {rate} fps, {size}."
    if transcript_present:
        return sentence + " A transcript is attached, so filler can be judged against the words."
    return (
        sentence
        + " No transcript is attached, so these notes are only what the XML can support."
    )


def _pace(sequences: list[Sequence], candidates: list[Candidate]) -> str:
    sections = section_pacing(sequences[0])
    if not sections or sum(item["shot_count"] for item in sections) == 0:
        return ""
    stretches = _stretches(sections)
    if len(stretches) == 1:
        group = stretches[0]
        start, end = group[0], group[-1]
        shots = sum(item["shot_count"] for item in group)
        average = _weighted_average(group)
        text = (
            f"Pace reads as one stretch"
            f" ({_tc_seconds(start['start_seconds'])}–{_tc_seconds(end['end_seconds'])}): "
            f"{shots} shots, averaging {_seconds(average)} "
            f"({start['cuts_per_minute']:.0f} cuts/min)."
        )
    else:
        parts = []
        for group in stretches:
            start, end = group[0], group[-1]
            shots = sum(item["shot_count"] for item in group)
            average = _weighted_average(group)
            parts.append(
                f"{_tc_seconds(start['start_seconds'])}–{_tc_seconds(end['end_seconds'])} "
                f"averages {_seconds(average)} a shot ({shots} shots)"
            )
        text = "Pace changes across the cut. " + "; ".join(parts) + "."
    shifts = [item for item in candidates if item.kind == "rhythm_shift"]
    if shifts:
        bits = []
        for item in shifts[:3]:
            before = item.signals.get("previous_asl_seconds")
            after = item.signals.get("local_asl_seconds")
            if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                bits.append(f"{_tc(item.timeline_start)} ({before:.1f}s shots to {after:.1f}s)")
            else:
                bits.append(_tc(item.timeline_start))
        text += " The rhythm breaks at " + _join(bits) + "."
    return text


def _mechanical(
    sequences: list[Sequence],
    candidates: list[Candidate],
    by_id: dict[str, Proposal],
) -> str:
    silences = [item for item in candidates if item.kind == "silence_gap"]
    covered = [item for item in candidates if item.kind == "covered_gap"]
    shorts = [item for item in candidates if item.kind == "short_clip"]
    shots = [
        clip
        for sequence in sequences
        for clip in sequence.spine
        if clip.kind != "gap" and clip.duration > 0
    ]
    parts: list[str] = []
    if silences:
        auto = [item for item in silences if _disposition(by_id, item) == "auto"]
        held = [item for item in silences if item not in auto]
        if auto:
            bits = [f"{_tc(item.timeline_start)} ({_span(item.duration)})" for item in auto]
            parts.append(
                "Bare primary, with nothing on a lane, at "
                + _join(bits)
                + ". Those are the silence cuts."
            )
        if held:
            bits = [f"{_tc(item.timeline_start)} ({_span(item.duration)})" for item in held]
            parts.append("Other bare stretches at " + _join(bits) + " stay in review.")
    elif len(shots) >= 8:
        parts.append("No bare primary gap of 1.25s or more.")
    if covered:
        bits = []
        for item in covered:
            count = item.signals.get("connected_count")
            bits.append(
                f"{_tc(item.timeline_start)} ({_span(item.duration)}, {count} connected)"
            )
        parts.append(
            "Covered primary stays in place at "
            + _join(bits)
            + ". The viewer is watching those connected clips, so the gap under them is left alone."
        )
    if shorts:
        flashes = [item for item in shorts if item.signals.get("flash")]
        others = [item for item in shorts if item not in flashes]
        if flashes:
            bits = [f"{item.clip_name} at {_tc(item.timeline_start)}" for item in flashes]
            parts.append("Flash frames: " + _join(bits) + ".")
        if others:
            bits = [
                f"{item.clip_name} at {_tc(item.timeline_start)} ({_span(item.duration)})"
                for item in others
            ]
            parts.append("Short clips under half a second: " + _join(bits) + ".")
    elif len(shots) >= 8:
        parts.append("No spine clip is under half a second, so there is no flash frame to lift.")
    return " ".join(parts)


def _holds(candidates: list[Candidate]) -> str:
    holds = [item for item in candidates if item.kind == "long_static"]
    if not holds:
        return ""
    bits = []
    for item in holds[:6]:
        ratio = item.signals.get("asl_ratio")
        if ratio:
            bits.append(
                f"{item.clip_name} at {_tc(item.timeline_start)} "
                f"({_span(item.duration)}, {ratio:.1f}× the shots around it)"
            )
        else:
            bits.append(f"{item.clip_name} at {_tc(item.timeline_start)} ({_span(item.duration)})")
    extra = f" {len(holds) - 6} more sit in the same list." if len(holds) > 6 else ""
    return (
        "Holds, measured against the shots beside them: "
        + _join(bits)
        + "."
        + extra
        + " These stay in review. A long travel shot can be the scene."
    )


def _stringout(candidates: list[Candidate]) -> str:
    runs = [item for item in candidates if item.kind == "untrimmed_run"]
    if not runs:
        return ""
    bits = []
    for item in runs:
        count = item.signals.get("shot_count")
        average = item.signals.get("average_seconds")
        other = item.signals.get("other_average_seconds")
        against = f", against {other:.1f}s in the rest of the cut" if other else ""
        bits.append(
            f"{count} shots from {_tc(item.timeline_start)} to {_tc(item.timeline_end)} "
            f"are the whole source clip (average {average:.1f}s{against})"
        )
    return (
        _join(bits)
        + ". No selects have been made there. It stays in the timeline for a person to cut down."
    )


def _cards(candidates: list[Candidate]) -> str:
    cards = [item for item in candidates if item.kind == "silent_card"]
    if not cards:
        return ""
    bits = [
        f"{item.clip_name} at {_tc(item.timeline_start)} ({_span(item.duration)})"
        for item in cards
    ]
    return (
        "Silent generator cards, with no audio and nothing on a lane: "
        + _join(bits)
        + ". Review, not a lift. A title or a shorten belongs on them."
    )


def _music(candidates: list[Candidate]) -> str:
    tails = [item for item in candidates if item.kind == "music_tail"]
    if not tails:
        return ""
    item = tails[0]
    tail = item.signals.get("tail_seconds")
    name = item.signals.get("bed_name") or "The music bed"
    amount = f"{tail:.0f}s" if tail is not None else "a while"
    return (
        f"{name} ends at {_tc(item.timeline_start)}, {amount} before the picture. "
        "The bed stays. Either finish with the song or tighten that tail."
    )


def _reuse(sequences: list[Sequence], candidates: list[Candidate]) -> str:
    reused = [item for item in candidates if item.kind == "source_reuse"]
    if not reused:
        return ""
    duration = sequences[0].duration or Fraction(0)
    starts = [item.timeline_start for item in reused]
    clustered = (
        len(reused) >= 4
        and duration > 0
        and min(starts) >= duration * Fraction(7, 10)
        and (max(starts) - min(starts)) <= Fraction(180)
    )
    names = []
    for item in reused:
        if item.clip_name not in names:
            names.append(item.clip_name)
    shown = ", ".join(names[:6])
    if len(names) > 6:
        shown += f", and {len(names) - 6} more"
    if clustered:
        return (
            f"From {_tc(min(starts))} the cut reprises earlier coverage "
            f"({len(reused)} shots, including {shown}). "
            "Marked for a look. A recap is a choice, so nothing here is lifted."
        )
    examples = []
    for item in reused[:3]:
        earlier = item.signals.get("earlier_start_seconds")
        when = _tc_seconds(earlier) if earlier is not None else "earlier"
        examples.append(f"{item.clip_name} at {_tc(item.timeline_start)} repeats {when}")
    return (
        "Repeated source ranges: "
        + _join(examples)
        + ". Marked for a look, and left in the cut."
    )


def _picture(sequences: list[Sequence], candidates: list[Candidate]) -> str:
    parts: list[str] = []
    for item in candidates:
        if item.kind != "colour_aspect":
            continue
        signals = item.signals
        parts.append(
            f"{item.clip_name} at {_tc(item.timeline_start)} is "
            f"{signals.get('clip_width')}×{signals.get('clip_height')} in a "
            f"{signals.get('sequence_width')}×{signals.get('sequence_height')} sequence"
            f"{_fit_note(signals)}. "
            "Marked for a person. The picture was not decoded."
        )
    mixes = [item for item in candidates if item.kind == "rate_mix"]
    if mixes:
        item = mixes[0]
        signals = item.signals
        parts.append(
            f"{signals.get('clip_count')} spine items are conformed from "
            f"{signals.get('source_rate')} into this {signals.get('sequence_rate')} sequence. "
            "That conform is already in the export, and it stays."
        )
    missing = [item for item in candidates if item.kind == "colour_role"]
    if missing:
        names = []
        for item in missing:
            if item.clip_name not in names:
                names.append(item.clip_name)
        shown = ", ".join(names[:4])
        if len(names) > 4:
            shown += f", and {len(names) - 4} more"
        parts.append(
            f"{len(missing)} spine items have no role on the clip or its audio ({shown})."
        )
    if not any(item.kind == "colour_unseen" for item in candidates):
        return " ".join(parts)
    if parts:
        parts.append("Exposure, white balance, and skin are not in the XML, so they were not judged.")
    elif sequences:
        parts.append(
            "Exposure, white balance, and skin are not in the XML, so they were not judged."
        )
    return " ".join(parts)


def _fit_note(signals: dict) -> str:
    bits = []
    rotation = signals.get("rotation")
    if rotation and str(rotation) not in {"0", "0.0"}:
        bits.append(f"rotated {rotation}°")
    scale = signals.get("scale")
    if scale and str(scale) not in {"1 1", "1.0 1.0", "1"}:
        bits.append(f"scaled {str(scale).split()[0]}×")
    if not bits:
        return ""
    return ", " + " and ".join(bits)


def _weighted_average(group: list[dict]) -> float:
    shots = sum(item["shot_count"] for item in group)
    if not shots:
        return 0.0
    total = sum(item["average_shot_seconds"] * item["shot_count"] for item in group)
    return total / shots


def _stretches(sections: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for section in sections:
        if not groups or _pace_changed(groups[-1][-1], section):
            groups.append([section])
        else:
            groups[-1].append(section)
    return groups


def _pace_changed(left: dict, right: dict) -> bool:
    before = float(left["average_shot_seconds"])
    after = float(right["average_shot_seconds"])
    if before <= 0 or after <= 0:
        return False
    ratio = after / before
    return ratio >= 1.6 or ratio <= 1 / 1.6


def _disposition(by_id: dict[str, Proposal], candidate: Candidate) -> str:
    proposal = by_id.get(candidate.id)
    if proposal is None:
        return ""
    return proposal.disposition


def _tc(pos: Fraction) -> str:
    return short_clock(pos)


def _tc_seconds(value: float | Fraction) -> str:
    return short_clock(Fraction(value))


def _span(value: Fraction) -> str:
    seconds = float(value)
    if seconds < 60:
        if abs(seconds - round(seconds)) < 0.05:
            return f"{int(round(seconds))}s"
        return f"{seconds:.1f}s"
    whole = int(seconds + 0.5)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _seconds(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}s"
    return f"{value:.1f}s"


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]

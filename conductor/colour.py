"""Colour pass. Review notes from the XML, never a grade.

Final Cut's XML already carries roles and frame sizes. It does not carry
exposure, white balance, or skin tone. This pass does not decode media, so
it cannot see the picture. The three checks are:

- ``colour_role`` — a spine clip (not a gap) with no audio role, video role,
  or role source.
- ``colour_aspect`` — the asset's format and the sequence format are both
  known, and the aspects disagree by a lot (portrait against landscape, or a
  relative difference of at least 15%).
- ``colour_unseen`` — one placeholder per sequence, hung on the first real
  clip, saying exposure and skin were not judged.

The pass is creative. ``gates.route`` will not give it ``auto``, and
``iterate`` only auto-applies the mechanical pass. Nothing here writes a
pixel.
"""

from __future__ import annotations

from collections.abc import Sequence as SequenceOf

from .candidates import Candidate
from .fcpxml import Clip, Sequence
from .transcript import Cue

#: Relative aspect gap that counts as extreme when orientation already matches.
#: 16:9 against 4:3 is about 0.25. A few pixels of width are not.
EXTREME_ASPECT = 0.15

_LABEL = {
    "colour_role": "role",
    "colour_aspect": "aspect",
    "colour_unseen": "exposure and skin",
}


def generate(
    sequences: SequenceOf[Sequence],
    _cues: list[Cue],
    _transcript_present: bool,
) -> list[Candidate]:
    """XML-only colour notes. Transcript cues are not consulted."""
    found: list[Candidate] = []
    for sequence in sequences:
        found.extend(_sequence(sequence))
    return found


def _sequence(sequence: Sequence) -> list[Candidate]:
    spine = [clip for clip in sequence.spine if clip.kind != "gap"]
    if not spine and not sequence.spine:
        return []
    found: list[Candidate] = []
    anchor = spine[0] if spine else sequence.spine[0]
    found.append(_unseen(sequence, anchor))
    for clip in spine:
        if clip.role is None:
            found.append(_role(sequence, clip))
        aspect = _aspect(sequence, clip)
        if aspect is not None:
            found.append(aspect)
    return found


def _unseen(sequence: Sequence, clip: Clip) -> Candidate:
    return _candidate(
        sequence,
        clip,
        kind="colour_unseen",
        reason=(
            "Exposure, white balance, and skin tone are not in the FCPXML. "
            "This pass did not decode the picture, so this is a placeholder "
            "for a person, not a grade."
        ),
        signals={"check": "exposure_skin", "decoded_media": False},
    )


def _role(sequence: Sequence, clip: Clip) -> Candidate:
    return _candidate(
        sequence,
        clip,
        kind="colour_role",
        reason=(
            f"{clip.name!r} has no audio role, video role, or role source. "
            "The colour pass cannot tell dialogue from coverage without that, "
            "and it did not decode the picture."
        ),
        signals={"check": "missing_role", "decoded_media": False},
    )


def _aspect(sequence: Sequence, clip: Clip) -> Candidate | None:
    if (
        sequence.width is None
        or sequence.height is None
        or clip.width is None
        or clip.height is None
        or sequence.height <= 0
        or clip.height <= 0
    ):
        return None
    how, relative = _mismatch(sequence.width, sequence.height, clip.width, clip.height)
    if how is None:
        return None
    orientation = "portrait" if clip.height > clip.width else "landscape"
    sequence_orientation = "portrait" if sequence.height > sequence.width else "landscape"
    if how == "orientation":
        why = (
            f"asset is {clip.width}×{clip.height} ({orientation}) and the sequence "
            f"is {sequence.width}×{sequence.height} ({sequence_orientation})"
        )
    else:
        why = (
            f"asset is {clip.width}×{clip.height} and the sequence is "
            f"{sequence.width}×{sequence.height} (relative aspect gap {relative:.2f})"
        )
    return _candidate(
        sequence,
        clip,
        kind="colour_aspect",
        reason=(
            f"{why}. That mismatch is already in the XML. The picture was not decoded."
        ),
        signals={
            "check": "aspect",
            "decoded_media": False,
            "mismatch": how,
            "sequence_width": sequence.width,
            "sequence_height": sequence.height,
            "clip_width": clip.width,
            "clip_height": clip.height,
            "relative_delta": round(relative, 4),
        },
    )


def _mismatch(
    seq_w: int, seq_h: int, clip_w: int, clip_h: int
) -> tuple[str | None, float]:
    seq_aspect = seq_w / seq_h
    clip_aspect = clip_w / clip_h
    relative = abs(seq_aspect - clip_aspect) / max(seq_aspect, clip_aspect)
    if (seq_h > seq_w) != (clip_h > clip_w):
        return "orientation", relative
    if relative >= EXTREME_ASPECT:
        return "ratio", relative
    return None, relative


def _candidate(sequence: Sequence, clip: Clip, *, kind: str, reason: str, signals: dict) -> Candidate:
    return Candidate(
        id="",
        kind=kind,
        label=_LABEL[kind],
        sequence=sequence.name,
        clip_id=clip.id,
        clip_name=clip.name,
        role=clip.role,
        timeline_start=clip.timeline_start,
        timeline_end=clip.timeline_end,
        reason=reason,
        transcript="",
        signals=signals,
        span="clip",
        pass_name="colour",
    )

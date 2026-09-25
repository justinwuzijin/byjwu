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
from .fcpxml import Clip, Sequence, local
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
        if not _has_role(clip):
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


def _has_role(clip: Clip) -> bool:
    """True when this spine item, or the audio inside it, carries a role.

    A connected title's ``videoRole`` belongs to the title, so it does not
    clear the parent. A compound clip often keeps ``dialogue.dialogue-1`` on
    the nested ``<audio>`` or ``<audio-channel-source>`` rather than on the
    outer element. That is the clip's own role.
    """
    if clip.role:
        return True
    element = clip.element
    if element is None:
        return False
    for node in element.iter():
        if node is element:
            continue
        tag = local(node.tag)
        if tag in {"audio-role-source", "video-role-source", "audio-channel-source"} and node.get("role"):
            return True
        if tag == "audio" and (node.get("role") or node.get("audioRole")):
            return True
        if node.get("lane"):
            continue
        if tag in {"asset-clip", "clip", "video", "audio"} and (
            node.get("audioRole") or node.get("videoRole")
        ):
            return True
    return False


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
    transform = _transform(clip)
    fitted = _fit_phrase(transform)
    if fitted:
        why = f"{why}. It is {fitted}"
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
            "rotation": transform.get("rotation"),
            "scale": transform.get("scale"),
        },
    )


def _transform(clip: Clip) -> dict[str, str]:
    element = clip.element
    if element is None:
        return {}
    for child in element:
        if local(child.tag) != "adjust-transform":
            continue
        found = {}
        if child.get("rotation"):
            found["rotation"] = child.get("rotation") or ""
        if child.get("scale"):
            found["scale"] = child.get("scale") or ""
        return found
    return {}


def _fit_phrase(transform: dict[str, str]) -> str:
    bits: list[str] = []
    rotation = transform.get("rotation")
    if rotation and rotation not in {"0", "0.0"}:
        bits.append(f"rotated {rotation}°")
    scale = transform.get("scale")
    if scale and scale not in {"1 1", "1.0 1.0", "1"}:
        amount = scale.split()[0]
        bits.append(f"scaled {amount}×")
    if not bits:
        return ""
    if len(bits) == 1:
        return bits[0]
    return f"{bits[0]} and {bits[1]}"


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

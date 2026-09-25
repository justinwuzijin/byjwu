"""Named editorial passes.

A pass is a slice of the timeline a room can run on its own. v1 implements
mechanical, dialogue, pacing, and a review-only colour scaffold. ``story``,
``audio`` and ``broll`` are registered so a later bot has a place to hang a
generator without inventing a new pipeline.

::

    register_pass(
        "story",
        kinds=frozenset(),
        creative=True,
        implemented=True,
        summary="Beat and story checks.",
        generate=my_generator,
    )

``generate`` is ``(sequences, cues, transcript_present) -> list[Candidate]``.
Leave it unset to reuse the spine heuristics filtered by ``kinds``.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence as SequenceOf
from dataclasses import dataclass

from .candidates import AudioSilence, Candidate, assign_ids, generate
from .colour import generate as colour_generate
from .errors import ConductorError
from .fcpxml import Sequence
from .transcript import Cue

Generator = Callable[[SequenceOf[Sequence], list[Cue], bool], list[Candidate]]


@dataclass(frozen=True)
class Pass:
    name: str
    kinds: frozenset[str]
    creative: bool
    implemented: bool
    summary: str
    generate: Generator | None = None


def _builtin(name: str, kinds: frozenset[str], creative: bool, summary: str) -> Pass:
    return Pass(name, kinds, creative, True, summary, None)


#: Execution order when the caller does not name a subset.
ORDER: tuple[str, ...] = ("mechanical", "dialogue", "pacing", "colour")

PASSES: dict[str, Pass] = {
    "mechanical": _builtin(
        "mechanical",
        frozenset({"silence_gap", "short_clip"}),
        False,
        "Bare silence on the primary storyline, quiet audio inside a clip when "
        "the file can be read, and clips under half a second. A gap that is "
        "covered by a connected clip is not a cut. The only pass eligible for "
        "an unattended apply.",
    ),
    "dialogue": _builtin(
        "dialogue",
        frozenset({"filler_pause"}),
        True,
        "Transcript filler and the pauses around it. Creative: review unless "
        "a person accepts the id.",
    ),
    "pacing": _builtin(
        "pacing",
        frozenset(
            {
                "long_static",
                "covered_gap",
                "source_reuse",
                "rhythm_shift",
                "rate_mix",
                "untrimmed_run",
                "silent_card",
                "music_tail",
            }
        ),
        True,
        "Holds against the local pace, gaps sitting under connected clips, "
        "repeated source ranges, sudden rhythm or frame-rate changes, an "
        "untrimmed string-out, a silent generator card, and a music bed that "
        "ends before the picture. Creative: review unless accepted. Those "
        "notes are markers, not lifts.",
    ),
    "story": Pass(
        "story",
        frozenset(),
        True,
        False,
        "Reserved. Story beats and cold-open structure. Register a generator.",
    ),
    "audio": Pass(
        "audio",
        frozenset(),
        True,
        False,
        "Reserved. Breaths, room tone, and clipped words. Register a generator.",
    ),
    "broll": Pass(
        "broll",
        frozenset(),
        True,
        False,
        "Reserved. Coverage against the A-roll. Register a generator.",
    ),
    "colour": Pass(
        "colour",
        frozenset({"colour_role", "colour_aspect", "colour_unseen"}),
        True,
        True,
        "Missing roles and extreme aspect mismatches already in the XML, plus "
        "a placeholder for exposure and skin. Review only: the picture is not "
        "decoded, and this pass is never an unattended cut.",
        colour_generate,
    ),
}


def register_pass(
    name: str,
    *,
    kinds: frozenset[str],
    creative: bool,
    implemented: bool,
    summary: str,
    generate: Generator | None = None,
) -> None:
    """Install or replace a pass. Tests that call this should restore ``PASSES``."""
    PASSES[name] = Pass(name, kinds, creative, implemented, summary, generate)


def is_creative(name: str) -> bool:
    spec = PASSES.get(name)
    if spec is None:
        raise ConductorError(f"unknown pass {name!r}")
    return spec.creative


def resolve_names(requested: Sequence[str] | None) -> list[str]:
    """Default is the implemented passes, in order. Unknown names error."""
    if not requested:
        return list(ORDER)
    names: list[str] = []
    for name in requested:
        if name not in PASSES:
            known = ", ".join(PASSES)
            raise ConductorError(f"unknown pass {name!r}. Known: {known}")
        if not PASSES[name].implemented:
            raise ConductorError(
                f"pass {name!r} is not implemented. "
                "It is an extension point: register_pass(...) with a generator."
            )
        if name not in names:
            names.append(name)
    return names


def collect(
    sequences: Sequence[Sequence],
    cues: list[Cue],
    *,
    transcript_present: bool,
    requested: Sequence[str] | None = None,
    audio_silences: SequenceOf[AudioSilence] | None = None,
) -> list[Candidate]:
    """Run the named passes and number the candidates in timeline order."""
    names = resolve_names(requested)
    pool: list[Candidate] = []
    for sequence in sequences:
        pool.extend(
            generate(
                sequence,
                cues,
                transcript_present=transcript_present,
                audio_silences=audio_silences,
            )
        )
    found: list[Candidate] = []
    for name in names:
        spec = PASSES[name]
        if spec.generate is not None:
            batch = list(spec.generate(sequences, cues, transcript_present))
        else:
            batch = [item for item in pool if item.kind in spec.kinds]
        for item in batch:
            item.pass_name = name
        found.extend(batch)
    found.sort(key=lambda item: (item.timeline_start, item.timeline_end, item.kind, item.pass_name))
    return assign_ids(found)

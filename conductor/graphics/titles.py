"""Where section titles go, and which distortion each one uses.

Both are taste calls, so they go through ``Router.decide`` as Opus types
(``title_placement``, ``title_treatment``). Code only lists the candidates:
a chapter marker, or the first spine item after a silence of at least
``section_pause_seconds``. With no Opus answer the candidate is still placed,
using the profile's default treatment, and the clip gets a review marker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from ..fcpxml import Sequence, local
from ..router import Ask, Decision, Router
from ..timeutil import parse_time, seconds
from .profile import GraphicsProfile, TREATMENTS


@dataclass
class TitleCard:
    id: str
    sequence: str
    text: str
    timeline_start: Fraction
    clip_id: str
    treatment: str = "blur_in"
    placed: bool = False
    needs_review: bool = False
    decided_by: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "text": self.text,
            "start_seconds": seconds(self.timeline_start),
            "clip_id": self.clip_id,
            "treatment": self.treatment,
            "placed": self.placed,
            "needs_review": self.needs_review,
            "decided_by": dict(self.decided_by),
        }


def title_candidates(sequence: Sequence, profile: GraphicsProfile, prefix: str) -> list[TitleCard]:
    pause = Fraction(profile.text_fx.section_pause_seconds).limit_denominator(1000)
    found: list[TitleCard] = []
    previous_end: Fraction | None = None
    for clip in sequence.spine:
        if clip.duration <= 0 or clip.element is None:
            previous_end = clip.timeline_end
            continue
        chapter = _chapter(clip.element)
        gap = previous_end is not None and clip.timeline_start - previous_end >= pause
        opening = not found and previous_end is None
        if chapter or gap or opening:
            raw = chapter or clip.name or "title"
            found.append(
                TitleCard(
                    id=f"{prefix}{len(found) + 1:03d}",
                    sequence=sequence.name,
                    text=_case(_clip_text(raw, profile.text_fx.max_chars), profile.typography.casing),
                    timeline_start=clip.timeline_start,
                    clip_id=clip.id,
                )
            )
        previous_end = clip.timeline_end
    return found[: profile.text_fx.max_titles]


def plan_titles(
    sequence: Sequence,
    profile: GraphicsProfile,
    router: Router,
    *,
    brief: str = "",
    ledger=None,
    id_prefix: str = "t",
) -> tuple[list[TitleCard], list[Decision], list[dict]]:
    cards = title_candidates(sequence, profile, id_prefix)
    if not cards:
        return [], [], []
    fx = profile.text_fx
    context = {"text_fx": {"treatments": list(fx.treatments), "default": fx.default_treatment}}
    place_asks = [
        Ask(
            id=f"{card.id}_place",
            type="title_placement",
            subject={"text": card.text, "at_seconds": seconds(card.timeline_start), "sequence": card.sequence},
            options={"place": "Put this title on screen.", "skip": "Leave this section untitled."},
            question="Does this section earn an on-screen title?",
            mock=(lambda index=index: ("place" if index < 3 else "skip", 0.55)),
        )
        for index, card in enumerate(cards)
    ]
    treat_asks = [
        Ask(
            id=f"{card.id}_fx",
            type="title_treatment",
            subject={"text": card.text, "treatments": list(fx.treatments)},
            options={name: name for name in fx.treatments},
            question="Which distortion treatment fits this title?",
            mock=(lambda chosen=fx.default_treatment if fx.default_treatment in fx.treatments else fx.treatments[0]: (chosen, 0.55)),
        )
        for card in cards
    ]
    decisions, receipts = router.decide([*place_asks, *treat_asks], brief=brief, context=context, ledger=ledger)
    by_id = {item.id: item for item in decisions}
    placed: list[TitleCard] = []
    for card, place_ask, treat_ask in zip(cards, place_asks, treat_asks, strict=True):
        place = by_id[place_ask.id]
        treat = by_id[treat_ask.id]
        card.decided_by = {
            "placement": f"{place.engine}/{place.source}",
            "treatment": f"{treat.engine}/{treat.source}",
        }
        card.needs_review = place.needs_review or treat.needs_review
        if place.value == "skip" and not place.needs_review:
            continue
        card.placed = True
        chosen = treat.value if treat.value in TREATMENTS else fx.default_treatment
        card.treatment = chosen if chosen in fx.treatments else fx.default_treatment
        placed.append(card)
    return placed, decisions, receipts


def _chapter(element) -> str | None:
    for child in element:
        if local(child.tag) == "chapter-marker" and child.get("value"):
            return child.get("value")
    return None


def _clip_text(text: str, limit: int) -> str:
    words = text.split()
    kept: list[str] = []
    for word in words:
        trial = " ".join([*kept, word])
        if len(trial) > limit:
            break
        kept.append(word)
    return " ".join(kept) or text[:limit]


def _case(text: str, casing: str) -> str:
    if casing == "lower":
        return text.lower()
    if casing == "upper":
        return text.upper()
    return text

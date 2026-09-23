"""Confidence gates. Jev proposes an action; this decides who may act on it.

Thresholds are configuration, not prompt text.

- ``auto`` — high confidence, low risk, and a mechanical pass. Eligible for
  ``--min-confidence`` apply.
- ``review`` — a real signal, but a person should look. Creative passes
  (dialogue, pacing, and anything registered as creative) land here even when
  the model is sure.
- ``escalate`` — low confidence, or Jev already said escalate.

The raw action is kept on the proposal. The gated action is what the marker
shows and what an unattended apply is allowed to perform.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gates:
    auto_confidence: float = 0.80
    review_confidence: float = 0.55
    auto_risk_max: float = 0.35

    def to_dict(self) -> dict[str, float]:
        return {
            "auto_confidence": self.auto_confidence,
            "review_confidence": self.review_confidence,
            "auto_risk_max": self.auto_risk_max,
        }


def route(
    raw_action: str,
    confidence: float,
    risk: float,
    *,
    creative: bool,
    gates: Gates,
) -> tuple[str, str]:
    """Return ``(gated_action, disposition)``.

    Disposition is ``keep``, ``auto``, ``review``, or ``escalate``.
    """
    if raw_action == "keep":
        return "keep", "keep"
    if raw_action == "escalate" or confidence < gates.review_confidence:
        return "escalate", "escalate"
    if raw_action == "mark_review":
        return "mark_review", "review"
    # tighten or remove from here.
    unattended = (
        not creative
        and confidence >= gates.auto_confidence
        and risk <= gates.auto_risk_max
    )
    if unattended:
        return raw_action, "auto"
    return "mark_review", "review"

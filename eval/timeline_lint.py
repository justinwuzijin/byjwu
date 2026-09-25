"""Synthetic golden plans and the tampers the linter must catch.

No real exports, media, or machine paths. The critic is a fixed function so
the ranking does not call a model.
"""

from __future__ import annotations

from fractions import Fraction

from conductor.critic import best_of
from conductor.lint import lint_plan
from conductor.plan import CUTAWAY, MUSIC, SPINE, TITLE, EditPlan, PlanClip, PlanWord

FRAME = Fraction(1, 24)
PROFILE = {
    "bands": {
        "median_shot_seconds": [1.5, 6.0],
        "cuts_per_minute": [4.0, 30.0],
        "broll_coverage": [0.15, 0.8],
        "longest_hold_seconds": [1.5, 8.0],
        "sentence_end_share": [0.5, 1.0],
    }
}


def _clip(role, name, start, end, **kwargs) -> PlanClip:
    return PlanClip(
        id=name,
        role=role,
        name=name,
        timeline_start=Fraction(start),
        timeline_end=Fraction(end),
        **kwargs,
    )


def golden_plan() -> EditPlan:
    """A 4-second plan that passes every hard check and the sample bands."""
    return EditPlan(
        name="Golden",
        frame_duration=FRAME,
        width=1920,
        height=1080,
        duration=Fraction(4),
        expects_music=True,
        clips=[
            _clip(
                SPINE, "open", 0, 2,
                source_start=Fraction(0), source_end=Fraction(2),
                asset_id="a", text="We left at dawn.", has_audio=True,
            ),
            _clip(
                SPINE, "turn", 2, 4,
                source_start=Fraction(2), source_end=Fraction(4),
                asset_id="a", text="The road was empty.", has_audio=True,
            ),
            _clip(
                CUTAWAY, "road", 0, 1,
                source_start=Fraction(0), source_end=Fraction(1),
                asset_id="b", lane=1,
            ),
            _clip(
                MUSIC, "bed", 0, 4,
                source_start=Fraction(0), source_end=Fraction(4),
                asset_id="m", fade_in=FRAME, fade_out=FRAME, has_audio=True,
            ),
            _clip(TITLE, "card", 0, 2, position=(0.0, 0.0), text="Dawn"),
        ],
        words=[
            PlanWord("dawn.", Fraction(1), Fraction(2)),
            PlanWord("road", Fraction(2), Fraction(3)),
            PlanWord("empty.", Fraction(3), Fraction(4)),
        ],
    )


def shift_cut(plan: EditPlan, delta: Fraction) -> EditPlan:
    """Move the cut between the two spine shots by ``delta`` seconds."""
    copied = _copy(plan)
    left, right = [clip for clip in copied.clips if clip.role == SPINE]
    left.timeline_end += delta
    left.source_end = (left.source_end or left.timeline_end) + delta
    right.timeline_start += delta
    right.source_start = (right.source_start or right.timeline_start) + delta
    return copied


def duplicate_clip(plan: EditPlan) -> EditPlan:
    copied = _copy(plan)
    road = next(clip for clip in copied.clips if clip.name == "road")
    copied.clips.append(
        _clip(
            CUTAWAY, "road-again", 1, 2,
            source_start=road.source_start, source_end=road.source_end,
            asset_id=road.asset_id, lane=1,
        )
    )
    return copied


def mid_word_cut(plan: EditPlan) -> EditPlan:
    copied = _copy(plan)
    left, right = [clip for clip in copied.clips if clip.role == SPINE]
    # "The" occupies 2.0–2.5. Land the cut inside it.
    left.timeline_end = Fraction(9, 4)
    left.source_end = Fraction(9, 4)
    right.timeline_start = Fraction(9, 4)
    right.source_start = Fraction(9, 4)
    return copied


def drop_cutaway(plan: EditPlan) -> EditPlan:
    copied = _copy(plan)
    copied.clips = [clip for clip in copied.clips if clip.role != CUTAWAY]
    return copied


def critic_for(original: EditPlan):
    """Rank the untouched plan at 5 and every other plan at 2."""
    original_name = original.summary()["kept_sentences"]

    def critic(payload: dict) -> dict:
        same = payload["plan"]["kept_sentences"] == original_name and not payload["lint"]["blocked"]
        # A tampered plan that still has the same sentences (a duplicated clip,
        # a missing cutaway, a shifted cut that keeps the text) is ranked by
        # whether the lint blocked it or the clip count changed.
        intact = (
            payload["plan"]["kept_sentences"] == original_name
            and not payload["lint"]["hard"]
            and _clip_names(payload) == _clip_names({"plan": original.summary()})
        )
        score = 5 if intact else 2
        return _verdict(score)

    return critic


def assert_tamper_caught(plan: EditPlan, *, code: str, soft: bool = False) -> None:
    report = lint_plan(plan, profile=PROFILE, check_media=False)
    findings = report.soft if soft else report.hard
    assert any(item.code == code for item in findings), report.to_dict()


def _verdict(score: int) -> dict:
    return {
        "action": "approve",
        "scores": {
            "pacing": score,
            "clip_selection": score,
            "visual_script": score,
            "story_arc": score,
        },
        "span": [],
        "cut_id": "",
        "reason": "",
    }


def _clip_names(payload: dict) -> list[str]:
    return [clip["name"] for clip in payload["plan"]["clips"]]


def _copy(plan: EditPlan) -> EditPlan:
    return EditPlan(
        name=plan.name,
        frame_duration=plan.frame_duration,
        width=plan.width,
        height=plan.height,
        duration=plan.duration,
        expects_music=plan.expects_music,
        clips=[
            PlanClip(**{key: getattr(clip, key) for key in clip.__dataclass_fields__ if key != "element"})
            for clip in plan.clips
        ],
        words=[PlanWord(word.text, word.start, word.end, word.sequence) for word in plan.words],
        cuts=list(plan.cuts),
        keypoints=list(plan.keypoints),
    )


def harmony_plan() -> EditPlan:
    """Visual cuts on a 120 BPM grid. Spine cuts are dialogue and sit off the beat."""
    beats = [i * 0.5 for i in range(9)]
    return EditPlan(
        name="Harmony",
        frame_duration=FRAME,
        width=1920,
        height=1080,
        duration=Fraction(4),
        expects_music=True,
        keypoints=beats,
        clips=[
            _clip(
                SPINE, "talk", 0, 4,
                source_start=Fraction(0), source_end=Fraction(4),
                asset_id="a", text="Hold this line.", has_audio=True,
            ),
            _clip(
                CUTAWAY, "insert", 0, 4,
                source_start=Fraction(0), source_end=Fraction(4),
                asset_id="b", lane=1,
            ),
            _clip(
                MUSIC, "bed", 0, 4,
                source_start=Fraction(0), source_end=Fraction(4),
                asset_id="m", fade_in=FRAME, fade_out=FRAME, has_audio=True,
            ),
        ],
        words=[PlanWord("Hold", Fraction(1, 5), Fraction(1))],
    )


def shift_cutaway_offbeat(plan: EditPlan) -> EditPlan:
    """Pull the cutaway out off the nearest keypoint, still on a frame."""
    copied = _copy(plan)
    insert = next(clip for clip in copied.clips if clip.role == CUTAWAY)
    insert.timeline_end = Fraction(15, 4)  # 3.75s, 0.25s from 3.5 and from 4.0
    insert.source_end = Fraction(15, 4)
    return copied


def rank_snapped_over_offbeat() -> int:
    snapped = harmony_plan()
    return best_of(
        [snapped, shift_cutaway_offbeat(snapped)],
        critic=_harmony_critic(),
        profile=PROFILE,
    )


def _harmony_critic():
    def critic(payload: dict) -> dict:
        fraction = payload["lint"]["stats"].get("av_harmony")
        score = 5 if fraction == 1 else 2
        return _verdict(score)

    return critic


def rank_original_first() -> int:
    original = golden_plan()
    plans = [
        original,
        shift_cut(original, Fraction(1, 2)),
        duplicate_clip(original),
        mid_word_cut(original),
        drop_cutaway(original),
    ]
    return best_of(plans, critic=critic_for(original), profile=PROFILE)

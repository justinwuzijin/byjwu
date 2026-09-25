"""Timeline linter, critic gate, and the tamper harness."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from conductor.critic import CRITIC_SCHEMA, best_of, review_plan
from conductor.lint import lint_plan
from conductor.plan import CUTAWAY, MUSIC, SPINE, TITLE, EditPlan, PlanClip, PlanWord, plan_from_document
from conductor.schema import SchemaError, validate
from eval.timeline_lint import (
    PROFILE,
    assert_tamper_caught,
    drop_cutaway,
    duplicate_clip,
    golden_plan,
    mid_word_cut,
    rank_original_first,
    shift_cut,
)

FRAME = Fraction(1, 24)


def _plan(clips, **kwargs) -> EditPlan:
    duration = kwargs.pop("duration", None)
    if duration is None:
        duration = max(clip.timeline_end for clip in clips)
    return EditPlan(
        name="Fixture",
        frame_duration=kwargs.pop("frame_duration", FRAME),
        width=kwargs.pop("width", 1920),
        height=kwargs.pop("height", 1080),
        duration=duration,
        clips=clips,
        **kwargs,
    )


def _spine(name, start, end, asset="a", source=None, text="") -> PlanClip:
    src = source if source is not None else (Fraction(start), Fraction(end))
    return PlanClip(
        id=name, role=SPINE, name=name,
        timeline_start=Fraction(start), timeline_end=Fraction(end),
        source_start=src[0], source_end=src[1], asset_id=asset,
        text=text, has_audio=True,
    )


def test_golden_plan_is_clean():
    report = lint_plan(golden_plan(), profile=PROFILE, check_media=False)
    assert report.hard == []
    assert report.soft == []
    assert report.blocked is False


def test_shift_duplicate_midword_and_missing_cutaway_are_caught():
    original = golden_plan()
    assert_tamper_caught(shift_cut(original, Fraction(1, 2)), code="mid_word")
    assert_tamper_caught(shift_cut(original, Fraction(-1, 2)), code="mid_word")
    assert_tamper_caught(duplicate_clip(original), code="source_overlap")
    assert_tamper_caught(mid_word_cut(original), code="mid_word")
    assert_tamper_caught(drop_cutaway(original), code="broll_coverage", soft=True)


def test_mocked_critic_ranks_the_original_first():
    assert rank_original_first() == 0


def test_flash_frame_under_two_frames():
    plan = _plan([_spine("flash", 0, Fraction(1, 24))])
    report = lint_plan(plan, check_media=False)
    assert any(item.code == "flash_frame" for item in report.hard)


def test_two_frames_is_not_a_flash():
    plan = _plan([_spine("shot", 0, Fraction(2, 24))])
    report = lint_plan(plan, check_media=False)
    assert not any(item.code == "flash_frame" for item in report.hard)


def test_unaligned_time_and_negative_time():
    clip = _spine("odd", 0, 1)
    clip.timeline_end = Fraction(1, 10)
    report = lint_plan(_plan([clip], duration=Fraction(1, 10)), check_media=False)
    assert any(item.code == "time_align" for item in report.hard)
    clip.timeline_start = Fraction(-1, 24)
    report = lint_plan(_plan([clip]), check_media=False)
    assert any(item.code == "time_invalid" for item in report.hard)


def test_audio_gap_under_a_cutaway_unless_intended():
    plan = _plan([
        _spine("talk", 0, 2),
        PlanClip(
            id="b", role=CUTAWAY, name="b", timeline_start=Fraction(2), timeline_end=Fraction(3),
            source_start=Fraction(0), source_end=Fraction(1), asset_id="b", lane=1,
        ),
    ])
    report = lint_plan(plan, check_media=False)
    assert any(item.code == "audio_gap" for item in report.hard)
    plan.clips[1].intended_gap = True
    assert not any(item.code == "audio_gap" for item in lint_plan(plan, check_media=False).hard)


def test_music_must_cover_and_fade():
    plan = _plan([
        _spine("talk", 0, 2),
        PlanClip(
            id="bed", role=MUSIC, name="bed", timeline_start=Fraction(0), timeline_end=Fraction(1),
            asset_id="m", has_audio=True, fade_in=Fraction(0), fade_out=Fraction(0),
        ),
    ], expects_music=True)
    codes = {item.code for item in lint_plan(plan, check_media=False).hard}
    assert "music_coverage" in codes
    assert "music_fade" in codes


def test_title_outside_the_safe_area():
    plan = _plan([
        _spine("talk", 0, 2),
        PlanClip(
            id="card", role=TITLE, name="card", timeline_start=Fraction(0), timeline_end=Fraction(1),
            position=(900.0, 0.0),
        ),
    ])
    assert any(item.code == "title_safe" for item in lint_plan(plan, check_media=False).hard)


def test_missing_media_and_a_resolved_file(tmp_path: Path):
    media = tmp_path / "clip.mov"
    media.write_bytes(b"not-a-real-movie")
    present = _spine("talk", 0, 1)
    present.asset_src = media.as_uri()
    missing = _spine("other", 1, 2, asset="b", source=(Fraction(0), Fraction(1)))
    missing.asset_src = (tmp_path / "gone.mov").as_uri()
    report = lint_plan(_plan([present, missing]), check_media=True)
    assert any(item.code == "media_missing" and item.clip_id == "other" for item in report.hard)
    assert not any(item.clip_id == "talk" and item.code == "media_missing" for item in report.hard)


def test_missing_profile_band_is_skipped_and_a_band_can_warn():
    plan = golden_plan()
    skipped = lint_plan(plan, profile=None, check_media=False)
    assert "median_shot_seconds" in skipped.skipped
    assert skipped.soft == []
    tight = {"bands": {"median_shot_seconds": [10.0, 12.0]}}
    warned = lint_plan(plan, profile=tight, check_media=False)
    assert any(item.code == "median_shot_seconds" for item in warned.soft)
    assert "cuts_per_minute" in warned.skipped


def test_repeated_point_uses_tfidf():
    plan = _plan([
        _spine("a", 0, 2, text="The train to the city was late."),
        _spine("b", 2, 4, asset="a", source=(Fraction(4), Fraction(6)), text="The train to the city was late."),
    ])
    assert any(item.code == "repeated_point" for item in lint_plan(plan, check_media=False).soft)


def test_critic_flag_veto_and_skip_and_cannot_add():
    from conductor.plan import PlanCut

    plan = golden_plan()
    plan.cuts = [PlanCut("c1", Fraction(2), Fraction(3), "silence")]

    def flag(_payload):
        return {
            "action": "flag",
            "scores": {"pacing": 3, "clip_selection": 3, "visual_script": 4, "story_arc": 4},
            "span": [0.0, 1.0],
            "cut_id": "",
            "reason": "the open holds too long",
        }

    flagged = review_plan(plan, critic=flag, check_media=False)
    assert flagged.blocked is False
    assert flagged.flags[0]["reason"] == "the open holds too long"
    assert len(flagged.turns) == 1

    rebuilt = []

    def veto(_payload):
        return {
            "action": "veto",
            "scores": {"pacing": 2, "clip_selection": 2, "visual_script": 2, "story_arc": 2},
            "span": [],
            "cut_id": "c1",
            "reason": "keep the pause",
        }

    def drop(cut_id):
        rebuilt.append(cut_id)
        plan.cuts = [cut for cut in plan.cuts if cut.id != cut_id]
        return plan

    vetoed = review_plan(plan, critic=veto, check_media=False, on_veto=drop)
    assert rebuilt == ["c1"]
    assert vetoed.vetoed == ["c1"]
    assert len(vetoed.turns) <= 2

    skipped = review_plan(plan, check_media=False)
    assert skipped.turns[0].skipped is True
    assert skipped.turns[0].reason == "no taste model available"

    with pytest.raises(SchemaError):
        validate({"action": "add", "scores": flag({})["scores"], "span": [], "cut_id": "", "reason": ""}, CRITIC_SCHEMA)


def test_best_of_rejects_a_hard_failure_and_picks_the_higher_score():
    good = golden_plan()
    bad = duplicate_clip(good)

    def critic(payload):
        score = 5 if not payload["lint"]["hard"] else 1
        return {
            "action": "approve",
            "scores": {key: score for key in ("pacing", "clip_selection", "visual_script", "story_arc")},
            "span": [],
            "cut_id": "",
            "reason": "",
        }

    assert best_of([bad, good], critic=critic) == 1


def test_two_rounds_is_the_maximum():
    plan = golden_plan()
    calls = {"n": 0}

    def always_veto(_payload):
        calls["n"] += 1
        return {
            "action": "veto",
            "scores": {"pacing": 1, "clip_selection": 1, "visual_script": 1, "story_arc": 1},
            "span": [],
            "cut_id": f"c{calls['n']}",
            "reason": "no",
        }

    def keep(cut_id):
        return plan

    review_plan(plan, critic=always_veto, check_media=False, on_veto=keep)
    assert calls["n"] == 2


def test_sample_apply_still_exports(tmp_path: Path):
    from conductor.run import analyze

    report = analyze(
        Path("fixtures/sample_interview.fcpxml"),
        transcript_path=Path("fixtures/sample_interview.srt"),
        brief="A tight interview. Keep the guest's story, lose dead air.",
        out_dir=tmp_path,
        apply=True,
        min_confidence=0.8,
        passes=["mechanical"],
    )
    assert report.out_applied is not None
    assert report.payload["lint"]["blocked"] is False
    assert "Critic skipped" in report.out_markdown.read_text(encoding="utf-8")


def test_plan_from_document_reads_spine_and_connected():
    from conductor.fcpxml import parse_fcpxml

    plan = plan_from_document(parse_fcpxml("fixtures/sample_interview.fcpxml"))
    roles = {clip.role for clip in plan.clips}
    assert "spine" in roles
    assert plan.frame_duration == Fraction(1, 24)

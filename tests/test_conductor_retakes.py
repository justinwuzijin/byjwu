"""Retake selection and word-boundary hygiene. Synthetic words only."""

from __future__ import annotations

from fractions import Fraction

from conductor.apply import Deletion, apply_edits
from conductor.decide import word_cuts
from conductor.fcpxml import parse_xml
from conductor.hygiene import hygienize, margins_for
from conductor.rules import plan_retakes, propose_fillers
from conductor.router import Router
from conductor.style import StyleProfile

FRAME = Fraction(1001, 30000)


class Word:
    def __init__(self, text, start, end, confidence=None, clip_id="clip", partial=False):
        self.text = text
        self.start = Fraction(start)
        self.end = Fraction(end)
        self.confidence = confidence
        self.clip_id = clip_id
        self.partial = partial
        self.sequence = "Cut"


def _words(line: str, start: Fraction, step: Fraction = Fraction("0.3")) -> list[Word]:
    words = []
    cursor = Fraction(start)
    for token in line.split():
        words.append(Word(token, cursor, cursor + step))
        cursor += step
    return words


def test_false_start_is_cut():
    first = _words("so today we're going", 0)
    second = _words("so today we're going to Tokyo", first[-1].end + Fraction("0.8"))
    plan = plan_retakes(first + second)
    assert len(plan.groups) == 1
    assert plan.cuts
    assert plan.cuts[0]["start"] == first[0].start
    assert plan.cuts[0]["end"] == second[0].start
    assert plan.cuts[0]["reason"] == "retake"
    assert "Tokyo" not in plan.cuts[0]["text"]


def test_later_complete_take_is_kept():
    first = _words("the train leaves for kyoto at noon", 0)
    second = _words("the train leaves for kyoto this noon", first[-1].end + 20)
    plan = plan_retakes(first + second)
    assert len(plan.groups) == 1
    kept = plan.groups[0].takes[plan.groups[0].keep]
    assert kept.start == second[0].start
    assert plan.cuts[0]["start"] == first[0].start


def test_same_line_outside_the_window_is_not_grouped():
    first = _words("the train leaves for kyoto at noon", 0)
    second = _words("the train leaves for kyoto at noon", first[-1].end + 90)
    plan = plan_retakes(first + second)
    assert plan.groups == []
    assert plan.cuts == []


def test_refrain_veto_keeps_both():
    first = _words("we go again when the lights come up", 0)
    second = _words("we go again when the lights come up", first[-1].end + 10)
    planned = plan_retakes(first + second)
    assert planned.cuts
    vetoed = plan_retakes(
        first + second,
        vetoes={planned.cuts[0]["id"]: "It is a refrain."},
    )
    assert vetoed.cuts == []
    assert vetoed.markers[0]["value"] == "Retake kept"
    assert "Both takes kept." in vetoed.markers[0]["note"]


def test_incomplete_last_take_keeps_the_second():
    full = "we are going to tokyo tomorrow morning"
    first = _words(full, 0)
    second = _words(full, first[-1].end + 5)
    third = _words("we are going to", second[-1].end + 5)
    plan = plan_retakes(first + second + third)
    assert len(plan.groups) == 1
    kept = plan.groups[0].takes[plan.groups[0].keep]
    assert kept.start == second[0].start
    assert third[0].start in {cut["start"] for cut in plan.cuts}


def test_clip_without_word_timings_is_skipped():
    class Bare:
        text = "hello"
        clip_id = "bare"

    assert plan_retakes([Bare()]).cuts == []
    assert plan_retakes([]).markers == []


def test_router_veto_default_allows_the_logic_cut():
    first = _words("the train leaves for kyoto at noon", 0)
    second = _words("the train leaves for kyoto this noon", first[-1].end + 20)
    with Router(live=False) as router:
        deletions, markers = word_cuts(
            first + second,
            sequence="Cut",
            frame_duration=Fraction(1, 24),
            router=router,
        )
    assert deletions
    assert all(marker["kind"] != "retake_veto" for marker in markers)


def test_spine_has_no_overlap_and_edges_sit_on_words():
    first = _words("so today we're going", 1, Fraction(1, 4))
    second = _words("so today we're going to Tokyo", first[-1].end + Fraction(1), Fraction(1, 4))
    words = first + second
    end = second[-1].end + 1
    plan = plan_retakes(words)
    cuts = hygienize(
        plan.cuts,
        words,
        profile=StyleProfile.from_dict({"pre_roll": 0, "post_roll": 0, "min_clip": 0}),
        frame_duration=Fraction(1, 24),
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <fcpxml version="1.11">
      <resources>
        <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="r2" name="Clip" src="file:///media/clip.mov" hasVideo="1" hasAudio="1" start="0s" duration="{end}s"/>
      </resources>
      <project name="Cut">
        <sequence format="r1" duration="{end}s" tcStart="0s">
          <spine>
            <asset-clip ref="r2" offset="0s" name="Clip" start="0s" duration="{end}s"/>
          </spine>
        </sequence>
      </project>
    </fcpxml>
    """
    document = parse_xml(xml)
    deletions = [
        Deletion(cut["id"], "Cut", cut["start"], cut["end"], "remove", "dialogue")
        for cut in cuts
    ]
    apply_edits(document, deletions)
    spine = document.sequences[0].spine
    ranges = [(clip.timeline_start, clip.timeline_end) for clip in spine]
    for left, right in zip(ranges, ranges[1:]):
        assert left[1] <= right[0]
    edges = [moment for span in ranges for moment in span]
    for edge in edges:
        assert all(not (word.start < edge < word.end) for word in words)


def test_gap_shorten_and_long_gap_removed():
    profile = StyleProfile()
    frame = Fraction(1, 50)
    short = hygienize(
        [{"id": "g1", "kind": "dead_air", "start": Fraction(0), "end": Fraction("0.9")}],
        profile=profile,
        frame_duration=frame,
    )
    long = hygienize(
        [{"id": "g2", "kind": "dead_air", "start": Fraction(0), "end": Fraction("1.5")}],
        profile=profile,
        frame_duration=frame,
    )
    assert short[0]["end"] - short[0]["start"] == Fraction("0.9") - Fraction("0.18")
    assert long[0]["end"] - long[0]["start"] == Fraction("1.5")


def test_min_cut_dropped_and_island_absorbed():
    frame = Fraction(1, 10)
    dropped = hygienize(
        [{"id": "tiny", "kind": "retake", "start": Fraction(0), "end": Fraction("0.1"), "explicit_trim": True}],
        frame_duration=frame,
    )
    absorbed = hygienize(
        [
            {"id": "a", "kind": "retake", "start": Fraction(0), "end": Fraction(1), "explicit_trim": True},
            {"id": "b", "kind": "retake", "start": Fraction("1.2"), "end": Fraction(2), "explicit_trim": True},
        ],
        frame_duration=frame,
    )
    assert dropped == []
    assert len(absorbed) == 1
    assert absorbed[0]["start"] == 0
    assert absorbed[0]["end"] == 2


def test_margins_clamp_when_words_are_close():
    post, pre = margins_for(Fraction(0), Fraction("0.15"))
    assert post + pre == Fraction("0.15")
    assert post < Fraction("0.20")
    assert pre < Fraction("0.12")
    words = [Word("left", 0, Fraction("0.4")), Word("right", Fraction("0.55"), Fraction(1))]
    cuts = hygienize(
        [{"id": "gap", "kind": "retake", "start": Fraction("0.4"), "end": Fraction("0.55"), "explicit_trim": True}],
        words,
        frame_duration=Fraction(1, 100),
    )
    assert cuts == [] or cuts[0]["start"] >= words[0].end


def test_out_point_inside_a_word_moves_to_the_word_end():
    words = [Word("going", 1, Fraction("1.5"))]
    cuts = hygienize(
        [{"id": "mid", "kind": "note", "start": Fraction("0.5"), "end": Fraction("1.2")}],
        words,
        frame_duration=Fraction(1, 10),
    )
    assert cuts[0]["end"] == Fraction("1.5")


def test_output_times_are_frame_multiples():
    cuts = hygienize(
        [{"id": "odd", "kind": "retake", "start": Fraction("0.013"), "end": Fraction("0.94"), "explicit_trim": True}],
        frame_duration=FRAME,
    )
    assert cuts
    for cut in cuts:
        assert cut["start"] % FRAME == 0
        assert cut["end"] % FRAME == 0


def test_um_without_silence_stays():
    words = [
        Word("we", 0, Fraction("0.2")),
        Word("um", Fraction("0.21"), Fraction("0.35")),
        Word("left", Fraction("0.36"), Fraction("0.6")),
    ]
    proposals = propose_fillers(words)
    assert not any(item["text"] == "um" for item in proposals)
    spaced = [
        Word("we", 0, Fraction("0.2")),
        Word("um", Fraction("0.4"), Fraction("0.55")),
        Word("left", Fraction("0.8"), Fraction(1)),
    ]
    heard = propose_fillers(spaced)
    assert any(item["text"] == "um" and item["auto"] for item in heard)


def test_dissolve_only_when_a_second_was_removed():
    profile = StyleProfile.from_dict({"allow_dissolves": True})
    frame = Fraction(1, 24)
    long = hygienize(
        [{"id": "long", "kind": "retake", "start": 0, "end": Fraction(2), "explicit_trim": True}],
        profile=profile,
        frame_duration=frame,
    )
    short = hygienize(
        [{"id": "short", "kind": "retake", "start": 0, "end": Fraction("0.5"), "explicit_trim": True}],
        profile=profile,
        frame_duration=frame,
    )
    blocked = hygienize(
        [{"id": "long", "kind": "retake", "start": 0, "end": Fraction(2), "explicit_trim": True}],
        profile=StyleProfile(),
        frame_duration=frame,
    )
    assert long[0]["transition"] == "dissolve"
    assert short[0]["transition"] is None
    assert blocked[0]["transition"] is None


def test_style_defaults_match_the_spec():
    profile = StyleProfile()
    assert profile.utterance_pause == Fraction("0.35")
    assert profile.group_window == 45
    assert profile.similarity == 0.6
    assert profile.prefix_tokens == 3
    assert profile.incomplete_coverage == 0.70
    assert profile.gap_target == Fraction("0.18")
    assert profile.pre_roll == Fraction("0.12")
    assert profile.post_roll == Fraction("0.20")
    assert profile.min_cut == Fraction("0.20")
    assert profile.min_clip == Fraction("0.30")
    assert profile.filler_silence == Fraction("0.08")
    assert profile.dead_air_shorten_min == Fraction("0.5")
    assert profile.dead_air_remove_min == Fraction("1.25")
    assert profile.dissolve_min_removed == 1
    assert profile.allow_dissolves is False

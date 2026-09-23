"""JSON, Markdown, and a small HTML page. The JSON is the room state payload.

Ranking is deterministic. ``auto`` calls (eligible to apply) come first, scored
by confidence × (1 − risk). Review and escalate follow, least confident first.
``keep`` is not a change.
"""

from __future__ import annotations

import json
from html import escape
from fractions import Fraction

from . import __version__
from .candidates import Candidate
from .decide import Proposal
from .fcpxml import Document, Sequence
from .timeutil import clock, seconds, smpte

PROTOCOL = "cut-conductor.room"
PROTOCOL_VERSION = 1


def build_payload(
    *,
    document: Document,
    sequences: list[Sequence],
    brief: str,
    mode: str,
    source_name: str,
    source_hash: str,
    transcript_name: str | None,
    cue_count: int,
    candidates: list[Candidate],
    proposals: list[Proposal],
    receipts: list[dict],
    markers_added: int,
    files: dict[str, str | None],
    passes: list[str],
    taste: dict,
    gates: dict,
    cuts: list[dict],
    apply_warnings: list[str],
    shadow: bool,
    applied: bool,
) -> dict:
    by_id = {item.id: item for item in candidates}
    proposal_by_id = {item.candidate_id: item for item in proposals}
    ranked = _rank(proposals, by_id, sequences)
    return {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "conductor_version": __version__,
        "mode": mode,
        "shadow": shadow,
        "applied": applied,
        "brief": brief,
        "passes": passes,
        "gates": gates,
        "taste": taste,
        "source": {
            "fcpxml": source_name,
            "blake2b": source_hash,
            "transcript": transcript_name,
            "cue_count": cue_count,
            "fcpxml_version": document.version,
        },
        "files": files,
        "assets": [
            {
                "id": asset.id,
                "name": asset.name,
                "src": asset.src,
                "duration_seconds": seconds(asset.duration),
                "has_video": asset.has_video,
                "has_audio": asset.has_audio,
            }
            for asset in document.assets.values()
        ],
        "sequences": [_sequence_summary(sequence) for sequence in sequences],
        "candidates": [item.to_state() for item in candidates],
        "changes": ranked,
        "kept": [
            _row(proposal_by_id[item.id], item, _sequence_for(sequences, item.sequence), section="kept", rank=None)
            for item in candidates
            if proposal_by_id[item.id].action == "keep"
        ],
        "markers_added": markers_added,
        "cuts": cuts,
        "apply_warnings": apply_warnings,
        "receipts": receipts,
    }


def render_markdown(payload: dict) -> str:
    lines = [
        f"# Cut Conductor — {_title(payload)}",
        "",
        _banner(payload),
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Passes: `{', '.join(payload['passes'])}`",
        f"- Model: `{_model(payload)}`",
        f"- Brief: {payload['brief']}",
        f"- Markers added this run: {payload['markers_added']}",
        f"- Cuts written: {len(payload['cuts'])}",
        "",
        "## Eligible to apply",
        "",
        "High-confidence mechanical calls. `--min-confidence` can cut these.",
        "",
        _table([row for row in payload["changes"] if row["section"] == "eligible"]),
        "",
        "## Review",
        "",
        "Creative calls, and mechanical calls that missed the auto gate. A person can `--accept` the id.",
        "",
        _table([row for row in payload["changes"] if row["section"] == "review"]),
        "",
        "## Escalate",
        "",
        _table([row for row in payload["changes"] if row["section"] == "escalate"]),
        "",
        "## Left as-is",
        "",
    ]
    kept = payload["kept"]
    if not kept:
        lines.append("No candidate was judged `keep`. Regions that were not candidates are untouched.")
    else:
        lines.append(_table(kept))
    lines.extend(
        [
            "",
            "## Marker colors",
            "",
            "FCPXML has no marker color attribute. The color is the `color=` field of the marker note, and the name starts with `CC`.",
            "",
            "| action | color | how it shows up in Final Cut |",
            "|---|---|---|",
            "| remove | red | standard marker, or a to-do if the call is uncertain |",
            "| tighten | blue | standard marker, or a to-do if the call is uncertain |",
            "| mark_review | orange | to-do marker (`completed=\"0\"`) |",
            "| escalate | purple | to-do marker (`completed=\"0\"`) |",
            "| keep | green | no marker |",
            "",
            "Full receipts (the state, the questions, and the answers) are in the JSON report.",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(payload: dict) -> str:
    """One self-contained page. No scripts, no remote assets."""
    def section(title: str, rows: list[dict]) -> str:
        body = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('rank') or ''))}</td>"
            f"<td>{escape(row['timecode'])}</td>"
            f"<td>{escape(row['action'])}</td>"
            f"<td>{escape(row['color'])}</td>"
            f"<td>{row['confidence']:.2f}</td>"
            f"<td>{row['risk']:.2f}</td>"
            f"<td>{escape(row['clip_name'])}</td>"
            f"<td>{escape(row['reason'])}</td>"
            "</tr>"
            for row in rows
        )
        if not body:
            body = "<tr><td colspan='8'>None</td></tr>"
        return (
            f"<h2>{escape(title)}</h2>"
            "<table><thead><tr><th>#</th><th>Timecode</th><th>Action</th>"
            "<th>Color</th><th>Conf</th><th>Risk</th><th>Clip</th><th>Why</th>"
            f"</tr></thead><tbody>{body}</tbody></table>"
        )

    proposed = [row for row in payload["changes"] if row["section"] == "eligible"]
    human = [row for row in payload["changes"] if row["section"] in {"review", "escalate"}]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cut Conductor — {escape(_title(payload))}</title>
<style>
body {{ font: 15px/1.45 Georgia, serif; margin: 2rem auto; max-width: 64rem; color: #1c1a16; background: #faf7f2; }}
table {{ border-collapse: collapse; width: 100%; background: white; }}
th, td {{ border-bottom: 1px solid #e4ddd2; text-align: left; padding: 0.4rem 0.5rem; vertical-align: top; }}
th {{ font: 12px/1.2 ui-sans-serif, sans-serif; letter-spacing: 0.04em; text-transform: uppercase; }}
.banner {{ background: #fff4d6; border: 1px solid #e6d39a; padding: 0.8rem 1rem; }}
</style>
</head>
<body>
<p class="banner">{escape(_banner(payload))} Mode: {escape(payload["mode"])}.</p>
<h1>Cut Conductor — {escape(_title(payload))}</h1>
<p>{escape(payload["brief"])}</p>
{section("Eligible to apply", proposed)}
{section("Review and escalate", human)}
</body>
</html>
"""


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _rank(
    proposals: list[Proposal], by_id: dict[str, Candidate], sequences: list[Sequence]
) -> list[dict]:
    buckets: dict[str, list[Proposal]] = {"eligible": [], "review": [], "escalate": []}
    for proposal in proposals:
        if proposal.action == "keep":
            continue
        if proposal.disposition == "auto":
            buckets["eligible"].append(proposal)
        elif proposal.disposition == "escalate":
            buckets["escalate"].append(proposal)
        else:
            buckets["review"].append(proposal)
    buckets["eligible"].sort(
        key=lambda item: (-(item.confidence * (1.0 - item.risk)), item.candidate_id)
    )
    for name in ("review", "escalate"):
        buckets[name].sort(key=lambda item: (item.confidence, -item.risk, item.candidate_id))
    rows = []
    for section, items in buckets.items():
        for rank, proposal in enumerate(items, start=1):
            candidate = by_id[proposal.candidate_id]
            rows.append(
                _row(proposal, candidate, _sequence_for(sequences, candidate.sequence), section, rank)
            )
    return rows


def _row(proposal: Proposal, candidate: Candidate, sequence: Sequence, section: str, rank: int | None) -> dict:
    origin = sequence.tc_start if sequence is not None else Fraction(0)
    frame = sequence.frame_duration if sequence is not None else Fraction(1, 24)
    return {
        "rank": rank,
        "section": section,
        "candidate_id": candidate.id,
        "fingerprint": candidate.to_state()["fingerprint"],
        "sequence": candidate.sequence,
        "clip_name": candidate.clip_name,
        "kind": candidate.kind,
        "pass": candidate.pass_name,
        "action": proposal.action,
        "raw_action": proposal.raw_action,
        "disposition": proposal.disposition,
        "eligible": proposal.eligible,
        "color": proposal.color,
        "confidence": proposal.confidence,
        "risk": proposal.risk,
        "needs_human": proposal.needs_human,
        "timecode": smpte(candidate.timeline_start, frame, origin),
        "range": [clock(candidate.timeline_start), clock(candidate.timeline_end)],
        "marker_name": proposal.marker_name,
        "reason": candidate.reason,
        "role": candidate.role,
    }


def _sequence_summary(sequence: Sequence) -> dict:
    return {
        "name": sequence.name,
        "event": sequence.event,
        "duration_seconds": seconds(sequence.duration) if sequence.duration is not None else None,
        "tc_start": str(sequence.tc_start),
        "frame_duration": str(sequence.frame_duration),
        "clip_count": len(sequence.spine),
        "existing_marker_count": sum(len(clip.markers) for clip in sequence.spine),
    }


def _sequence_for(sequences: list[Sequence], name: str) -> Sequence:
    for sequence in sequences:
        if sequence.name == name:
            return sequence
    return sequences[0]


def _banner(payload: dict) -> str:
    if payload.get("applied"):
        return (
            "Cuts were written to a new FCPXML. The source file was not modified. "
            "The shadow FCPXML still has proposal markers only."
        )
    return (
        "Shadow report. No timeline edits were applied. "
        "The companion FCPXML adds proposal markers only."
    )


def _title(payload: dict) -> str:
    names = [item["name"] for item in payload["sequences"]]
    return ", ".join(names) if names else "timeline"


def _model(payload: dict) -> str:
    receipts = payload.get("receipts") or []
    if not receipts:
        return "none"
    return str(receipts[0].get("model"))


def _table(rows: list[dict]) -> str:
    if not rows:
        return "_None._"
    header = "| # | timecode | pass | action | color | conf | risk | clip | why |"
    rule = "|---|---|---|---|---|---|---|---|---|"
    body = []
    for row in rows:
        body.append(
            "| "
            + " | ".join(
                [
                    _cell(row.get("rank") or ""),
                    _cell(row["timecode"]),
                    _cell(row.get("pass") or ""),
                    _cell(row["action"]),
                    _cell(row["color"]),
                    f"{row['confidence']:.2f}",
                    f"{row['risk']:.2f}",
                    _cell(row["clip_name"]),
                    _cell(row["reason"]),
                ]
            )
            + " |"
        )
    return "\n".join([header, rule, *body])


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")

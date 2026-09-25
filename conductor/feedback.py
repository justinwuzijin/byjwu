"""Feedback Conductor can read without an editor at the keyboard.

Two inputs, both JSON-or-FCPXML a room bot can write or drop:

- A diff between the shadow FCPXML Conductor handed back and the FCPXML
  the editor re-exported from Final Cut. Markers carry the suggestion.
  What happened to the picture decides accept, reject, or modify. A cut
  that matches no suggestion is an extra.
- A notes file. One object per thing the person said. ``at`` binds a
  timecode ("12:30") to a candidate. No ``at`` and a ``kind`` is a standing
  rule ("always cut flash frames").

Rejects are inferred from a deleted marker only when some other Conductor
marker survived the re-export. A file that lost every marker is treated as
an export that stripped notes, not as a reject-all.

Style parameters (pacing by section, music fade lengths, typography) are
not candidate kinds. A module that knows a ``styles/<name>/profile`` format
calls :func:`register_observer` with ``(proposed, edited) -> [observation]``.
Each observation needs a ``param`` and a ``value``. It lands in the log as an
``observe`` event, and it does not move a confidence prior.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from .errors import ConductorError
from .fcpxml import Document, Sequence, parse_fcpxml
from .taste import Taste, feedback_event, normalize_rule
from .timeutil import seconds

_SHADOW = "Cut Conductor shadow proposal"
_CLOCK = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$")
_SMPTE = re.compile(r"^(\d+):(\d{2}):(\d{2}):(\d{2})$")
_MINSEC = re.compile(r"^(\d+):(\d{2})$")
_FIELD = re.compile(r"(?:^|\|)\s*([A-Za-z0-9_]+)=([^|]*)")

Observer = Callable[[Document, Document], list[dict]]
OBSERVERS: dict[str, Observer] = {}


def register_observer(name: str, observer: Observer) -> None:
    """Add a re-export reader for style parameters. Same name replaces."""
    if not name:
        raise ConductorError("an observer needs a name")
    OBSERVERS[name] = observer


def _observations(proposed: Document, edited: Document) -> list[dict]:
    events: list[dict] = []
    for name, observer in OBSERVERS.items():
        for item in observer(proposed, edited) or []:
            if not isinstance(item, dict) or not item.get("param") or "value" not in item:
                raise ConductorError(f"observer {name!r} returned a row without param and value")
            row = {**item, "event": "observe", "source": "diff", "observer": name}
            row.setdefault(
                "fingerprint",
                _fingerprint(
                    "observe",
                    name,
                    item["param"],
                    item.get("section") or "",
                    json.dumps(item["value"], sort_keys=True, default=str),
                ),
            )
            events.append(row)
    return events


def diff_fcpxml(
    proposed: str | Path,
    edited: str | Path,
) -> tuple[list[dict], list[str]]:
    """Compare a conductor shadow file with an editor re-export.

    Returns ``(events, warnings)``. Events are ready for ``Taste.append``.
    """
    proposed_doc = parse_fcpxml(proposed)
    edited_doc = parse_fcpxml(edited)
    return diff_documents(proposed_doc, edited_doc)


def diff_documents(proposed: Document, edited: Document) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    pairs = _pair_sequences(proposed, edited)
    if not pairs:
        warnings.append("no sequence in the re-export matched the conductor output")
        return [], warnings
    events: list[dict] = []
    explained: list[tuple[str, Fraction, Fraction]] = []
    explained_gaps: list[tuple[str, Fraction]] = []
    suggestions = 0
    for proposed_seq, edited_seq in pairs:
        frame = proposed_seq.frame_duration or Fraction(1, 24)
        markers_live = _conductor_markers(edited_seq)
        survived = bool(markers_live)
        found = _suggestions(proposed, proposed_seq)
        suggestions += len(found)
        if found and not survived:
            warnings.append(
                f"{proposed_seq.name}: conductor markers did not survive the re-export; "
                "rejects were not inferred. Accepts, modifies, and extra cuts still are."
            )
        for suggestion in found:
            outcome = _outcome(
                suggestion,
                proposed,
                proposed_seq,
                edited,
                edited_seq,
                frame,
                survived,
            )
            if outcome is None:
                continue
            event, pieces = outcome
            events.append(event)
            if event["event"] in {"accept", "modify"}:
                explained.extend(pieces)
                for piece in suggestion["pieces"]:
                    if piece["type"] == "gap" and piece.get("left_key") and piece.get("left_src_end") is not None:
                        explained_gaps.append((piece["left_key"], piece["left_src_end"]))
        events.extend(
            _extras(
                proposed,
                proposed_seq,
                edited,
                edited_seq,
                frame,
                explained,
                explained_gaps,
                start_index=1 + sum(1 for event in events if event["event"] == "extra"),
            )
        )
    if suggestions == 0:
        warnings.append("no conductor markers in the proposed FCPXML")
    events.extend(_observations(proposed, edited))
    return events, warnings


def ingest_notes(
    taste: Taste,
    path: str | Path,
    *,
    candidates=None,
) -> tuple[list[dict], list[str]]:
    """Fold a room notes file into ``taste``. Timed notes wait in ``pending``."""
    learned, warnings = apply_note_items(taste, load_notes(path))
    if candidates is not None:
        bound, bind_warnings = bind_pending(taste, candidates)
        learned.extend(bound)
        warnings.extend(bind_warnings)
    return learned, warnings


def apply_note_items(taste: Taste, items: list[dict]) -> tuple[list[dict], list[str]]:
    """Store rules and direct events. Timecoded notes stay pending until bound."""
    learned: list[dict] = []
    warnings: list[str] = []
    for item in items:
        kind, row, warning = _classify_note(item)
        if warning:
            warnings.append(warning)
        if kind == "rule" and row is not None:
            if taste.add_rule(row):
                learned.append({**row, "record": "rule"})
        elif kind == "event" and row is not None:
            if row.get("at"):
                taste.add_pending(row)
            elif taste.append(row):
                learned.append(row)
    return learned, warnings


def bind_pending(taste: Taste, candidates) -> tuple[list[dict], list[str]]:
    """Attach pending timecodes to this run's candidates."""
    kept: list[dict] = []
    learned: list[dict] = []
    warnings: list[str] = []
    for item in taste.pending:
        if not item.get("at"):
            kept.append(item)
            continue
        bound, warning = bind_note(item, candidates)
        if warning:
            warnings.append(warning)
        if bound is None:
            kept.append(item)
            continue
        if taste.append(bound):
            learned.append(bound)
    taste.pending = kept
    return learned, warnings


def load_notes(path: str | Path) -> list[dict]:
    file = Path(path)
    if not file.is_file():
        raise ConductorError(f"no such feedback file: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConductorError(f"feedback file is not JSON: {file}: {exc}") from exc
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items", data.get("notes"))
    else:
        raise ConductorError("feedback file must be an object or a list")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ConductorError("feedback items must be a list of objects")
    return items


def parse_at(text: str, frame: Fraction = Fraction(1, 24)) -> Fraction:
    """``12:30`` is 12 minutes 30 seconds. ``750`` and ``750s`` are seconds."""
    raw = str(text).strip()
    if not raw:
        raise ConductorError("feedback timecode is empty")
    smpte = _SMPTE.fullmatch(raw)
    if smpte:
        hour, minute, sec, frames = (int(smpte.group(i)) for i in range(1, 5))
        return Fraction(hour * 3600 + minute * 60 + sec) + frame * frames
    clock = _CLOCK.fullmatch(raw)
    if clock:
        hour, minute, sec = (int(clock.group(i)) for i in range(1, 4))
        millis = int((clock.group(4) or "0").ljust(3, "0"))
        return Fraction(hour * 3600 + minute * 60 + sec) + Fraction(millis, 1000)
    short = _MINSEC.fullmatch(raw)
    if short:
        return Fraction(int(short.group(1)) * 60 + int(short.group(2)))
    if raw.endswith("s") and ":" not in raw:
        from .timeutil import parse_time

        try:
            return parse_time(raw)
        except ValueError as exc:
            raise ConductorError(f"unreadable feedback timecode: {text!r}") from exc
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return Fraction(raw)
    raise ConductorError(f"unreadable feedback timecode: {text!r}")


def bind_note(item: dict, candidates) -> tuple[dict | None, str | None]:
    try:
        when = parse_at(str(item.get("at")))
    except ConductorError as exc:
        return None, str(exc)
    kind = item.get("kind") if isinstance(item.get("kind"), str) else None
    match = _match_candidate(candidates, when, kind)
    if match is None:
        label = f" for {kind}" if kind else ""
        return None, f"no candidate near {item.get('at')}{label}"
    note = item.get("note") if isinstance(item.get("note"), str) else ""
    event = feedback_event(
        event=item.get("event") or "reject",
        candidate_id=match.id,
        action=item.get("action") or "",
        pass_name=match.pass_name,
        note=note,
        fields={
            "kind": match.kind,
            "at": item.get("at"),
            "source": "note",
            "fingerprint": item.get("fingerprint"),
            "timeline_start_seconds": seconds(match.timeline_start),
            "timeline_end_seconds": seconds(match.timeline_end),
            "clip_name": match.clip_name,
        },
    )
    return event, None


def _classify_note(item: dict) -> tuple[str, dict | None, str | None]:
    if item.get("at"):
        event_name = item.get("event") or "reject"
        if event_name not in {"accept", "reject", "modify"}:
            return "skip", None, f"timed note event must be accept, reject, or modify, got {event_name!r}"
        row = dict(item)
        row["event"] = event_name
        row["fingerprint"] = item.get("fingerprint") or _fingerprint(
            "note",
            event_name,
            item.get("at"),
            item.get("kind") or "",
            item.get("note") or "",
        )
        row["source"] = "note"
        return "event", row, None
    if item.get("candidate_id") or item.get("id"):
        try:
            event = feedback_event(
                event=item.get("event") or "reject",
                candidate_id=str(item.get("candidate_id") or item.get("id")),
                action=str(item.get("action") or ""),
                pass_name=str(item.get("pass") or item.get("pass_name") or ""),
                note=str(item.get("note") or ""),
                fields={
                    "kind": item.get("kind"),
                    "source": "note",
                    "fingerprint": item.get("fingerprint")
                    or _fingerprint(
                        "note",
                        item.get("event"),
                        item.get("candidate_id") or item.get("id"),
                        item.get("kind") or "",
                        item.get("note") or "",
                    ),
                },
            )
        except ConductorError as exc:
            return "skip", None, str(exc)
        return "event", event, None
    if item.get("kind") or item.get("scope") == "rule":
        try:
            rule = normalize_rule(item)
        except ConductorError as exc:
            return "skip", None, str(exc)
        return "rule", rule, None
    return "skip", None, "feedback item needs at, a candidate id, or a kind"


def _pair_sequences(proposed: Document, edited: Document) -> list[tuple[Sequence, Sequence]]:
    if len(proposed.sequences) == 1 and len(edited.sequences) == 1:
        return [(proposed.sequences[0], edited.sequences[0])]
    by_name = {sequence.name: sequence for sequence in edited.sequences}
    return [
        (sequence, by_name[sequence.name])
        for sequence in proposed.sequences
        if sequence.name in by_name
    ]


def _suggestions(document: Document, sequence: Sequence) -> list[dict]:
    found = []
    for clip in sequence.spine:
        for marker in clip.markers:
            note = marker.note or ""
            if _SHADOW not in note:
                continue
            fields = _note_fields(note)
            if not fields.get("id") or not fields.get("kind"):
                continue
            start, end = _range_or_clip(fields.get("range"), clip)
            found.append(
                {
                    "id": fields["id"],
                    "kind": fields["kind"],
                    "action": fields.get("action") or fields.get("raw_action") or "",
                    "pass": fields.get("pass") or "",
                    "start": start,
                    "end": end,
                    "clip_name": clip.name,
                    "pieces": _pieces(document, sequence, start, end, sequence.frame_duration),
                }
            )
    return found


def _outcome(suggestion, proposed, proposed_seq, edited, edited_seq, frame, survived):
    status = _range_status(suggestion, edited, edited_seq, frame)
    marker_here = any(marker["id"] == suggestion["id"] for marker in _conductor_markers(edited_seq))
    pieces = [
        (piece["key"], piece["src0"], piece["src1"])
        for piece in suggestion["pieces"]
        if piece["type"] == "media"
    ]
    if status == "gone":
        action = suggestion["action"] if suggestion["action"] in {"remove", "tighten"} else "remove"
        return _event("accept", suggestion, action, "the suggested range is gone"), pieces
    if status == "partial":
        action = suggestion["action"] if suggestion["action"] in {"remove", "tighten"} else "tighten"
        return _event("modify", suggestion, action, "the suggested range was only partly cut"), pieces
    if status == "kept" and not marker_here and survived:
        action = suggestion["action"] or "remove"
        return (
            _event("reject", suggestion, action, "marker removed and the picture is still there"),
            [],
        )
    return None


def _event(name: str, suggestion: dict, action: str, note: str) -> dict:
    return feedback_event(
        event=name,
        candidate_id=suggestion["id"],
        action=action,
        pass_name=suggestion["pass"],
        note=note,
        fields={
            "kind": suggestion["kind"],
            "source": "diff",
            "clip_name": suggestion["clip_name"],
            "timeline_start_seconds": seconds(suggestion["start"]),
            "timeline_end_seconds": seconds(suggestion["end"]),
            "fingerprint": _fingerprint(
                "diff",
                name,
                suggestion["id"],
                suggestion["kind"],
                seconds(suggestion["start"]),
                seconds(suggestion["end"]),
            ),
        },
    )


def _extras(proposed, proposed_seq, edited, edited_seq, frame, explained, explained_gaps, start_index):
    events = []
    index = start_index
    for clip in proposed_seq.spine:
        if clip.kind == "gap":
            left = _previous_media(proposed_seq, clip)
            if left is None:
                continue
            left_key = _media_key(proposed, left)
            left_end = left.start + left.duration
            if any(key == left_key and abs(end - left_end) <= frame for key, end in explained_gaps):
                continue
            remaining = _gap_after(edited, edited_seq, left_key, left_end, frame)
            if remaining is None or abs(remaining - clip.duration) <= frame or remaining > clip.duration + frame:
                continue
            if clip.duration <= frame:
                continue
            kind = "silence_gap" if clip.duration >= Fraction("1.25") else "editor_cut"
            removed = clip.duration - remaining
            events.append(
                _extra_event(
                    index,
                    kind,
                    "remove" if remaining <= frame else "tighten",
                    clip.name,
                    clip.timeline_start,
                    clip.timeline_start + removed,
                    {},
                    "gap removed that we did not suggest",
                )
            )
            index += 1
            continue
        key = _media_key(proposed, clip)
        spans = _spans(edited, edited_seq, key)
        removed = _subtract(clip.start, clip.start + clip.duration, spans, frame)
        for src0, src1 in removed:
            if _explained(key, src0, src1, explained):
                continue
            local0 = src0 - clip.start
            local1 = src1 - clip.start
            kind, signals = _extra_kind(clip, src1 - src0)
            whole = abs((src1 - src0) - clip.duration) <= frame
            events.append(
                _extra_event(
                    index,
                    kind,
                    "remove" if whole else "tighten",
                    clip.name,
                    clip.timeline_start + local0,
                    clip.timeline_start + local1,
                    signals,
                    "editor cut we did not suggest",
                )
            )
            index += 1
    return events


def _extra_event(index, kind, action, clip_name, start, end, signals, note) -> dict:
    fields = {
        "kind": kind,
        "source": "diff",
        "clip_name": clip_name,
        "timeline_start_seconds": seconds(start),
        "timeline_end_seconds": seconds(end),
        "fingerprint": _fingerprint("diff", "extra", kind, clip_name, seconds(start), seconds(end)),
    }
    if signals:
        fields["signals"] = signals
    return feedback_event(
        event="extra",
        candidate_id=f"extra-{index:04d}",
        action=action,
        pass_name="",
        note=note,
        fields=fields,
    )


def _extra_kind(clip, removed: Fraction) -> tuple[str, dict]:
    if clip.duration < Fraction("0.45"):
        signals = {"flash": True} if clip.duration < Fraction("0.20") else {}
        return "short_clip", signals
    if clip.duration >= 20 and removed >= Fraction(1):
        return "long_static", {}
    return "editor_cut", {}


def _range_status(suggestion, edited, edited_seq, frame) -> str:
    pieces = suggestion["pieces"]
    if not pieces:
        return "kept"
    states = [_piece_status(piece, edited, edited_seq, frame) for piece in pieces]
    if any(state == "partial" for state in states):
        return "partial"
    if all(state == "gone" for state in states):
        return "gone"
    if all(state == "kept" for state in states):
        return "kept"
    return "partial"


def _piece_status(piece, edited, edited_seq, frame) -> str:
    if piece["type"] == "gap":
        if not piece.get("left_key") or piece.get("left_src_end") is None:
            return "gone"
        remaining = _gap_after(edited, edited_seq, piece["left_key"], piece["left_src_end"], frame)
        if remaining is None:
            return "gone"
        return _gap_bucket(remaining, piece["duration"], frame)
    need = piece["src1"] - piece["src0"]
    if need <= 0:
        return "gone"
    kept = _overlap(piece["src0"], piece["src1"], _spans(edited, edited_seq, piece["key"]))
    ratio = kept / need
    if ratio >= Fraction(9, 10):
        return "kept"
    if ratio <= Fraction(1, 10):
        return "gone"
    return "partial"


def _gap_bucket(remaining: Fraction, original: Fraction, frame: Fraction) -> str:
    if abs(remaining - original) <= frame:
        return "kept"
    if remaining <= frame:
        return "gone"
    if remaining < original - frame:
        return "partial"
    return "kept"


def _gap_after(document, sequence, left_key, left_src_end, frame) -> Fraction | None:
    left = _clip_at_source_end(document, sequence, left_key, left_src_end, frame)
    if left is None:
        return None
    total = Fraction(0)
    seen = False
    for clip in sequence.spine:
        if clip is left:
            seen = True
            continue
        if not seen:
            continue
        if clip.kind == "gap":
            total += clip.duration
            continue
        break
    return total


def _clip_at_source_end(document, sequence, key, src_end, frame):
    found = []
    for clip in sequence.spine:
        if clip.kind == "gap" or _media_key(document, clip) != key:
            continue
        end = clip.start + clip.duration
        if abs(end - src_end) <= frame or clip.start - frame <= src_end <= end + frame:
            found.append(clip)
    if not found:
        return None
    return min(found, key=lambda clip: abs((clip.start + clip.duration) - src_end))


def _pieces(document, sequence, start, end, frame) -> list[dict]:
    frame = frame or Fraction(1, 24)
    pieces = []
    for clip in sequence.spine:
        lo = max(clip.timeline_start, start)
        hi = min(clip.timeline_end, end)
        if hi - lo <= frame / 2:
            continue
        if clip.kind == "gap":
            left = _previous_media(sequence, clip)
            pieces.append(
                {
                    "type": "gap",
                    "duration": hi - lo,
                    "left_key": _media_key(document, left) if left is not None else None,
                    "left_src_end": (left.start + left.duration) if left is not None else None,
                }
            )
            continue
        local = lo - clip.timeline_start
        src0 = clip.start + local
        pieces.append(
            {
                "type": "media",
                "key": _media_key(document, clip),
                "src0": src0,
                "src1": src0 + (hi - lo),
                "name": clip.name,
            }
        )
    return pieces


def _previous_media(sequence, gap):
    previous = None
    for clip in sequence.spine:
        if clip is gap:
            return previous
        if clip.kind != "gap":
            previous = clip
    return None


def _spans(document, sequence, key) -> list[tuple[Fraction, Fraction]]:
    spans = []
    for clip in sequence.spine:
        if clip.kind == "gap" or _media_key(document, clip) != key:
            continue
        spans.append((clip.start, clip.start + clip.duration))
    return _merge(spans)


def _merge(spans: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    ordered = sorted(spans)
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _overlap(start, end, spans) -> Fraction:
    total = Fraction(0)
    for left, right in spans:
        lo = max(start, left)
        hi = min(end, right)
        if hi > lo:
            total += hi - lo
    return total


def _subtract(start, end, spans, frame) -> list[tuple[Fraction, Fraction]]:
    cursor = start
    removed = []
    for left, right in spans:
        if right <= cursor:
            continue
        if left >= end:
            break
        if left > cursor:
            removed.append((cursor, min(left, end)))
        cursor = max(cursor, right)
        if cursor >= end:
            break
    if cursor < end:
        removed.append((cursor, end))
    return [(left, right) for left, right in removed if right - left > frame]


def _explained(key, start, end, explained) -> bool:
    need = end - start
    if need <= 0:
        return True
    covered = _overlap(start, end, [(a, b) for item_key, a, b in explained if item_key == key])
    return covered / need >= Fraction(9, 10)


def _conductor_markers(sequence: Sequence) -> list[dict]:
    found = []
    for clip in sequence.spine:
        for marker in clip.markers:
            note = marker.note or ""
            if _SHADOW not in note:
                continue
            fields = _note_fields(note)
            if fields.get("id"):
                found.append({"id": fields["id"], "note": note})
    return found


def _note_fields(note: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip() for match in _FIELD.finditer(note)}


def _range_or_clip(raw: str | None, clip) -> tuple[Fraction, Fraction]:
    if raw and "-" in raw:
        left, right = raw.split("-", 1)
        try:
            return _parse_clock(left), _parse_clock(right)
        except ConductorError:
            pass
    return clip.timeline_start, clip.timeline_end


def _parse_clock(text: str) -> Fraction:
    match = _CLOCK.fullmatch(text.strip())
    if not match:
        raise ConductorError(f"unreadable marker range: {text!r}")
    hour, minute, sec = (int(match.group(i)) for i in range(1, 4))
    millis = int((match.group(4) or "0").ljust(3, "0"))
    return Fraction(hour * 3600 + minute * 60 + sec) + Fraction(millis, 1000)


def _media_key(document: Document, clip) -> str:
    asset = document.assets.get(clip.ref or "")
    if asset is not None and asset.src:
        return "src:" + asset.src
    if asset is not None and asset.name:
        return "asset:" + asset.name
    return "clip:" + clip.name


def _match_candidate(candidates, when: Fraction, kind: str | None):
    pool = list(candidates)
    if kind:
        narrowed = [item for item in pool if item.kind == kind]
        if narrowed:
            pool = narrowed
    if not pool:
        return None
    containing = [item for item in pool if item.timeline_start <= when < item.timeline_end]
    if containing:
        return min(containing, key=lambda item: abs(item.timeline_start - when))
    nearest = min(pool, key=lambda item: abs(_mid(item) - when))
    if abs(_mid(nearest) - when) <= 2:
        return nearest
    return None


def _mid(candidate) -> Fraction:
    return (candidate.timeline_start + candidate.timeline_end) / 2


def _fingerprint(*parts) -> str:
    raw = "\0".join(str(part) for part in parts)
    return hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()

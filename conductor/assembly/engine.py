"""One assembly pass: raw material + music + brief + style → a finished FCPXML.

::

    result = assemble(media="~/Desktop/jevid-in", brief="an 8-minute video on ...",
                      style="byjustinwu", out_dir="~/Desktop/jevid-out/v0")

Writes ``<name>.assembled.fcpxml`` (DTD-checked when lxml is installed),
``assembly.json`` (protocol ``jevid.assembly``) and ``assembly.md``, plus the
background stills under ``assets/``. Source media is hashed before and after
and never written. Dry-run unless ``live`` is set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..errors import ConductorError
from ..fcpxml import write_document
from ..ingest import DEFAULT_BRIEF, load_duration_overrides, slug
from ..jev import dry_run_forced
from ..report import dumps
from ..router import Router, format_usage
from ..style import CUT_SECTIONS, StyleProfile, load_style
from ..taste import Taste, load_taste
from ..timeutil import clock
from ..validate import validate_fcpxml
from .dress import dress, timeline_beats
from .layout import Adjustments, Layout
from .media import Material, SignalProvider, assert_unchanged, gather
from .metrics import StyleTargets, measure
from .render import render
from .select import Decision, build_units, estimate_budgets, heuristics, select
from .timeline import Timeline

PROTOCOL = "jevid.assembly"
PROTOCOL_VERSION = 1


def _adopt(rows: list) -> dict:
    """Graphics router decisions, in the assembly decision shape."""
    from .select import Decision

    adopted = {}
    for row in rows:
        lane = "linear" if row.engine == "jev" else "creative"
        adopted[row.id] = Decision(
            key=row.id,
            item=row.id,
            field="value",
            value=row.value,
            confidence=float(row.confidence),
            reason=row.why,
            source=f"{row.engine}:{row.source}",
            model=row.model or "",
            review=bool(row.needs_review),
            lane=lane,
        )
    return adopted


def _speech_words(timeline: Timeline, units: list) -> list:
    """Cue text as evenly timed words on the sequence clock, for graphics subtitles."""
    from fractions import Fraction

    from ..graphics.subtitles import Word

    by_id = {unit.id: unit for unit in units}
    words: list[Word] = []
    for item in timeline.spine:
        unit = by_id.get(item.tags.get("unit"))
        if unit is None:
            continue
        for cue in unit.cues:
            tokens = str(cue.text).split()
            if not tokens or cue.end <= cue.start:
                continue
            step = (cue.end - cue.start) / len(tokens)
            for index, token in enumerate(tokens):
                src_start = cue.start + step * index
                src_end = cue.start + step * (index + 1)
                start = item.offset + (src_start - item.start)
                end = item.offset + (src_end - item.start)
                if end > start:
                    words.append(Word(token, Fraction(start), Fraction(end), ""))
    return words
_MINUTES = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(?:min|mins|minute|minutes)\b", re.IGNORECASE)
_SECONDS = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(?:s|sec|secs|second|seconds)\b", re.IGNORECASE)


@dataclass
class AssemblyResult:
    fcpxml: Path
    json: Path
    markdown: Path
    timeline: Timeline
    decisions: dict[str, Decision]
    dress_decisions: dict[str, Decision]
    metrics: dict
    failures: list[str]
    dtd_errors: list[str] | None
    payload: dict
    mode: str
    warnings: list[str] = field(default_factory=list)

    @property
    def review(self) -> list[Decision]:
        rows = list(self.decisions.values()) + list(self.dress_decisions.values())
        return [d for d in rows if d.review]


def parse_target(brief: str) -> float | None:
    """``"an 8-minute video"`` → 480.0, ``"a 45s teaser"`` → 45.0, else None."""
    match = _MINUTES.search(brief)
    if match:
        return float(match.group(1)) * 60.0
    match = _SECONDS.search(brief)
    if match:
        return float(match.group(1))
    return None


def assemble(
    *,
    brief: str,
    out_dir: str | Path,
    media: str | Path | None = None,
    fcpxml: str | Path | None = None,
    music: str | Path | list | tuple | None = None,
    style: str | Path | StyleProfile | None = None,
    taste_path: str | Path | None = None,
    taste: Taste | None = None,
    durations_path: str | Path | None = None,
    target_seconds: float | None = None,
    name: str | None = None,
    live: bool = False,
    transcript_path: str | Path | None = None,
    router: Router | None = None,
    adjustments: Adjustments | None = None,
    prior: dict[str, Decision] | None = None,
    material: Material | None = None,
    assets_dir: str | Path | None = None,
    signals: SignalProvider | None = None,
) -> AssemblyResult:
    brief = (brief or "").strip() or DEFAULT_BRIEF
    profile = style if isinstance(style, StyleProfile) else load_style(style)
    taste = taste or load_taste(taste_path)
    adjustments = adjustments or Adjustments()
    owned_router = router is None
    if router is None:
        router = Router(live=bool(live) and not dry_run_forced())
    use_live = router.live
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if material is None:
        overrides = load_duration_overrides(durations_path) if durations_path else None
        beats = profile.get("music.beats")
        material = gather(
            media=media,
            fcpxml=fcpxml,
            music=music,
            transcript_path=transcript_path,
            overrides=overrides,
            signals=signals,
            beats=bool(beats["detect"]),
            beat_range=(float(beats["min_bpm"]), float(beats["max_bpm"])),
            fallback_bpm=beats.get("fallback_bpm"),
            beats_per_bar=int(beats["beats_per_bar"]),
        )
    if not profile.get("format.follow_footage"):
        material.width = int(profile.get("format.width"))
        material.height = int(profile.get("format.height"))
    target = float(target_seconds or parse_target(brief) or profile.get("structure.target_seconds"))
    title = (name or (material.root.name if material.root else None) or "Assembly").strip() or "Assembly"
    warnings = list(material.warnings) + [f"style: {w}" for w in profile.warnings]
    if profile.provisional:
        warnings.append(
            f"style profile {profile.name} {profile.version} is PROVISIONAL: numbers are placeholders until the style study lands."
        )

    talking = profile.pacing("talking")
    talking_max = float(talking["shot_length"]["p90"]) * adjustments.asl_scale.get("talking", 1.0)
    units, unit_warnings = build_units(material, profile, talking_max=talking_max)
    warnings.extend(unit_warnings)
    if not units:
        raise ConductorError("no usable ranges in the footage; nothing to assemble")
    budgets = estimate_budgets(profile, target * adjustments.target_scale)
    hints = heuristics(units, profile, budgets, cover_scale=adjustments.cover_scale)
    decisions, receipts = select(
        units, hints, brief=brief, profile=profile, taste=taste, router=router, prior=prior
    )
    layout = Layout(
        material,
        profile,
        units,
        decisions,
        target=target,
        adjustments=adjustments,
        name=title,
    )
    timeline = layout.run()
    warnings.extend(layout.warnings)
    if not timeline.spine:
        raise ConductorError("assembly placed nothing; every range was dropped")
    assets = Path(assets_dir) if assets_dir else destination / "assets"
    dress_decisions, dress_receipts, dress_warnings = dress(
        timeline,
        material=material,
        profile=profile,
        units=units,
        decisions=decisions,
        segments=layout.segments,
        brief=brief,
        taste=taste,
        router=router,
        adjustments=adjustments,
        assets_dir=assets / "background",
        prior=prior,
    )
    warnings.extend(dress_warnings)
    out_fcpxml = destination / f"{slug(title)}.assembled.fcpxml"
    for path in material.hashes:
        if out_fcpxml.resolve() == path:
            raise ConductorError("refusing to write the assembly over a source file; choose another --out-dir")
    dissolve = profile.get("cuts.dissolve") or {}
    write_document(
        render(
            timeline,
            event=title,
            dissolve_sections=set(dissolve.get("sections") or []),
            dissolve_seconds=float(dissolve.get("duration_seconds") or 0),
        ),
        out_fcpxml,
    )
    graphic_decisions: dict = {}
    if profile.get("typography.subtitle.enabled") or profile.get("background.enabled"):
        from ..graphics import apply_graphics, load_graphics_profile

        graphic = apply_graphics(
            out_fcpxml,
            out_path=out_fcpxml,
            words=_speech_words(timeline, units),
            beats=[float(beat) for beat in timeline_beats(layout.segments)],
            profile=load_graphics_profile(profile.data),
            router=router,
            brief=brief,
            enabled=True,
            ledger=router.ledger,
        )
        warnings.extend(graphic.notes)
        graphic_decisions = _adopt(graphic.decisions)
    dress_decisions = {**dress_decisions, **graphic_decisions}
    dtd_errors = validate_fcpxml(out_fcpxml)
    if dtd_errors is None:
        warnings.append("lxml is not installed; the FCPXML was not checked against the DTD.")
    expect_subtitles = any(unit.cues for unit in units) and bool(profile.get("typography.subtitle.enabled"))
    metrics = measure(out_fcpxml, profile, expect_subtitles=expect_subtitles, beats=timeline_beats(layout.segments))
    targets = StyleTargets(profile, target * adjustments.target_scale if target_seconds or parse_target(brief) else None)
    failures = targets.failures(metrics)
    assert_unchanged(material)
    mode = "live" if use_live else "dry-run"
    usage = router.ledger.to_dict()
    payload = _payload(
        brief=brief.strip(),
        mode=mode,
        profile=profile,
        target=target,
        adjustments=adjustments,
        material=material,
        units=units,
        timeline=timeline,
        segments=layout.segments,
        decisions=decisions,
        dress_decisions=dress_decisions,
        receipts=receipts + dress_receipts,
        metrics=metrics,
        failures=failures,
        targets=targets.to_dict(),
        dtd_errors=dtd_errors,
        out_fcpxml=out_fcpxml,
        warnings=warnings,
        decision_usage=usage,
    )
    if owned_router:
        router.close()
    out_json = destination / "assembly.json"
    out_md = destination / "assembly.md"
    out_json.write_text(dumps(payload), encoding="utf-8")
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return AssemblyResult(
        fcpxml=out_fcpxml,
        json=out_json,
        markdown=out_md,
        timeline=timeline,
        decisions=decisions,
        dress_decisions=dress_decisions,
        metrics=metrics,
        failures=failures,
        dtd_errors=dtd_errors,
        payload=payload,
        mode=mode,
        warnings=warnings,
    )


def _usage_line(usage: dict) -> str:
    return format_usage(usage)


def _payload(**kw) -> dict:
    profile: StyleProfile = kw["profile"]
    timeline: Timeline = kw["timeline"]
    material: Material = kw["material"]
    connected = timeline.connected
    structure = []
    for section in timeline.sections:
        items = [i for i in timeline.spine if section.start <= i.offset < section.end]
        structure.append(
            {
                "kind": section.kind,
                "label": section.label,
                "start": clock(section.start),
                "start_seconds": round(float(section.start), 3),
                "duration_seconds": round(float(section.duration), 3),
                "budget_seconds": round(float(section.budget), 3),
                "spine_items": len(items),
                "cutaways": sum(1 for c in connected if c.tags.get("cutaway") and section.start <= c.offset < section.end),
                "subtitles": sum(1 for c in connected if c.tags.get("subtitle") and section.start <= c.offset < section.end),
                "background_stills": sum(1 for c in connected if c.kind == "still" and section.start <= c.offset < section.end),
            }
        )
    music = [
        {
            "song": c.name,
            "start": clock(c.offset),
            "duration_seconds": round(float(c.duration), 3),
            "lane": c.lane,
            "fade_in_seconds": c.tags.get("fade_in"),
            "fade_out_seconds": c.tags.get("fade_out"),
            "transition_in": c.tags.get("transition_in"),
            "transition_out": c.tags.get("transition_out"),
            "volume_keyframes": len(c.volume_keys),
        }
        for c in connected
        if c.tags.get("music")
    ]
    all_decisions = list(kw["decisions"].values()) + list(kw["dress_decisions"].values())
    return {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "conductor_version": __version__,
        "mode": kw["mode"],
        "brief": kw["brief"],
        "target_seconds": kw["target"],
        "style": {
            **profile.to_state(),
            "description": profile.get("description", ""),
            "assumptions": list(profile.get("assumptions", []) or []),
            "provenance": profile.get("provenance", {}),
        },
        "adjustments": kw["adjustments"].to_state(),
        "material": {
            "root": str(material.root) if material.root else None,
            "frame_duration": str(material.frame),
            "width": material.width,
            "height": material.height,
            "footage": [
                {
                    **f.clip.to_state(),
                    "role": f.role,
                    "role_reason": f.role_reason,
                    "signals": f.signals.source,
                    "cues": len(f.signals.cues),
                }
                for f in material.footage
            ],
            "songs": [song.to_state() for song in material.songs],
        },
        "units": [unit.to_state() for unit in kw["units"]],
        "timeline": {
            "name": timeline.name,
            "duration_seconds": round(float(timeline.duration), 3),
            "spine_items": len(timeline.spine),
            "connected_items": len(connected),
            "titles": sum(1 for c in connected if c.kind == "title"),
            "subtitles": sum(1 for c in connected if c.tags.get("subtitle")),
            "background_stills": sum(1 for c in connected if c.kind == "still"),
            "markers": sum(len(i.notes) for i in timeline.spine + connected),
            "todo_markers": sum(1 for i in timeline.spine + connected for n in i.notes if n.todo),
        },
        "structure": structure,
        "music": music,
        "decisions": [d.to_state() for d in all_decisions],
        "review": [d.to_state() for d in all_decisions if d.review],
        "receipts": kw["receipts"],
        "decision_usage": kw["decision_usage"],
        "decision_usage_summary": _usage_line(kw["decision_usage"]),
        "metrics": kw["metrics"],
        "targets": kw["targets"],
        "failures": kw["failures"],
        "dtd": {
            "checked": kw["dtd_errors"] is not None,
            "valid": kw["dtd_errors"] == [] if kw["dtd_errors"] is not None else None,
            "errors": kw["dtd_errors"] or [],
            "version": "1.11",
        },
        "files": {"fcpxml": str(kw["out_fcpxml"])},
        "warnings": kw["warnings"],
    }


def render_markdown(payload: dict) -> str:
    style = payload["style"]
    lines = [f"# jevid assembly — {payload['timeline']['name']}", ""]
    if style.get("provisional"):
        lines += [
            f"> **Provisional style.** `{style['name']}` {style['version']} is a placeholder until the style study lands.",
            "",
        ]
    lines += [
        f"- Brief: {payload['brief']}",
        f"- Mode: `{payload['mode']}` (live taste decisions use Grok 4.7; dry-run uses the local mock)",
        f"- Decisions: {payload.get('decision_usage_summary') or 'none'}",
        f"- Length: {payload['timeline']['duration_seconds']:.2f}s against a {payload['target_seconds']:.0f}s target",
        f"- FCPXML: `{payload['files']['fcpxml']}`",
        f"- DTD 1.11: {'valid' if payload['dtd']['valid'] else ('not checked' if not payload['dtd']['checked'] else 'INVALID')}",
        "",
        "## Structure",
        "",
        "| section | start | seconds | spine | cutaways | subtitles | background | ASL target | ASL measured |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    measured = {row["start_seconds"]: row for row in payload["metrics"]["sections"]}
    targets = payload["targets"]["asl_seconds"]
    for row in payload["structure"]:
        m = measured.get(row["start_seconds"], {})
        target = targets.get(row["kind"], "—") if row["kind"] in CUT_SECTIONS else "—"
        lines.append(
            f"| {row['label']} | {row['start']} | {row['duration_seconds']:.2f} | {row['spine_items']} | "
            f"{row['cutaways']} | {row['subtitles']} | {row['background_stills']} | {target} | {m.get('asl_seconds', '—')} |"
        )
    lines += ["", "## Music", ""]
    if payload["music"]:
        lines += ["| song | start | seconds | in | out | keyframes |", "|---|---|---|---|---|---|"]
        for row in payload["music"]:
            lines.append(
                f"| {row['song']} | {row['start']} | {row['duration_seconds']:.2f} | "
                f"{row['transition_in']} {row['fade_in_seconds']}s | {row['transition_out']} {row['fade_out_seconds']}s | {row['volume_keyframes']} |"
            )
    else:
        lines.append("No music.")
    metrics = payload["metrics"]
    lines += [
        "",
        "## Style metrics",
        "",
        f"- ASL by section: {metrics['asl_by_section']}",
        f"- On-beat cuts (sections marked `always`): {metrics['on_beat']['hit']}/{metrics['on_beat']['checked']}",
        f"- Music fades compliant: {metrics['music'].get('compliant')}",
        f"- Ducking: {metrics['ducking']['compliant']}/{metrics['ducking']['checked']} dialogue spans under the duck level",
        f"- Subtitle coverage: {metrics['subtitles']['coverage']}",
        f"- SF Pro compliance: {metrics['fonts']['compliance']}",
        f"- Background coverage: {metrics['background']['coverage']}",
        f"- Failing targets: {', '.join(payload['failures']) or 'none'}",
        "",
        "## Decisions to review",
        "",
    ]
    if payload["review"]:
        for row in payload["review"]:
            lines.append(f"- `{row['key']}` = {row['value']} ({row['confidence']:.2f}, {row['source']}): {row['reason']}")
    else:
        lines.append("None below the review gate.")
    if style.get("assumptions"):
        lines += ["", "## What the style profile assumes", ""]
        lines += [f"- {text}" for text in style["assumptions"]]
    if payload["warnings"]:
        lines += ["", "## Warnings", ""]
        lines += [f"- {text}" for text in payload["warnings"]]
    lines.append("")
    return "\n".join(lines)


"""Iterate an assembly: v0, then rounds that correct it against style metrics.

``v0/`` is the first assembly. Each later round measures the previous FCPXML
(``metrics.measure``), turns every failing style target into a parameter
change (:func:`refine`), and re-assembles into ``vN/``. Decisions carry over
by content signature, so a round does not re-ask the model about a range it
already judged; the material and beat grids are read once. Taste is reloaded
each round, so accept/reject events a room appends between rounds pin those
decisions (see ``select.taste_pins``).

Stop on the first of:

- ``metrics`` — every style target holds
- ``no-progress`` — the failing targets have no parameter left to turn
- ``max-rounds`` — the cap was hit with targets still failing

A person is needed when a target still fails at the end. Low-confidence
decisions are to-do markers in the file either way.
"""

from __future__ import annotations

from pathlib import Path

from .. import __version__
from ..errors import ConductorError
from ..ingest import load_duration_overrides
from ..iterate import IterateResult, PROTOCOL, PROTOCOL_VERSION
from ..report import dumps
from ..jev import dry_run_forced
from ..router import Router
from ..style import CUT_SECTIONS, StyleProfile, load_style
from ..taste import load_taste
from .engine import AssemblyResult, assemble, parse_target
from .layout import Adjustments
from .media import SignalProvider, gather
from .select import prior_index

_CLAMP = (0.4, 2.5)


def refine(
    adjustments: Adjustments, result: AssemblyResult, profile: StyleProfile
) -> tuple[Adjustments, list[str]]:
    """Map each failing target to a parameter change. Returns the new set and what moved."""
    metrics = result.metrics
    new = Adjustments.from_state(adjustments.to_state())
    changes: list[str] = []
    for failure in result.failures:
        if failure.startswith("asl:"):
            kind = failure.split(":", 1)[1]
            measured = float(metrics["asl_by_section"].get(kind) or 0.0)
            target = float(profile.pacing(kind)["asl_seconds"])
            if measured <= 0:
                continue
            ratio = target / measured
            before = new.asl_scale.get(kind, 1.0)
            after = min(max(before * ratio, _CLAMP[0]), _CLAMP[1])
            if abs(after - before) > 0.01:
                new.asl_scale[kind] = round(after, 4)
                changes.append(f"asl_scale.{kind} {before:.3f}→{after:.3f} (measured {measured:.2f}s, target {target:.2f}s)")
            if kind == "talking" and ratio < 1 and new.cover_scale < 2.0:
                new.cover_scale = round(min(2.0, new.cover_scale * 1.25), 4)
                changes.append(f"cover_scale→{new.cover_scale:.2f} (more cutaways)")
        elif failure == "duration":
            measured = float(metrics["duration_seconds"])
            wanted = float(result.payload["target_seconds"])
            if measured > 0:
                before = new.target_scale
                after = min(max(before * wanted / measured, 0.6), 1.6)
                if abs(after - before) > 0.01:
                    new.target_scale = round(after, 4)
                    changes.append(f"target_scale {before:.3f}→{after:.3f} (length {measured:.1f}s vs {wanted:.1f}s)")
        elif failure == "on_beat":
            if new.snap_scale < 4.0:
                new.snap_scale = round(min(4.0, new.snap_scale * 1.5), 4)
                changes.append(f"snap_scale→{new.snap_scale:.2f} (wider beat window)")
        elif failure == "subtitle_coverage":
            if not new.subtitle_fill:
                new.subtitle_fill = True
                changes.append("subtitle_fill on (cards bridge short gaps)")
    return new, changes


def iterate_assembly(
    *,
    brief: str,
    out_dir: str | Path,
    media: str | Path | None = None,
    fcpxml: str | Path | None = None,
    music: str | Path | None = None,
    style: str | Path | None = None,
    taste_path: str | Path | None = None,
    durations_path: str | Path | None = None,
    target_seconds: float | None = None,
    name: str | None = None,
    live: bool = False,
    max_rounds: int = 3,
    signals: SignalProvider | None = None,
    router: Router | None = None,
) -> IterateResult:
    if bool(media) == bool(fcpxml):
        raise ConductorError("assembly iterate needs exactly one of --media or --fcpxml")
    if max_rounds < 0:
        raise ConductorError("--max-rounds must be >= 0 for an assembly (0 writes v0 only)")
    profile = load_style(style)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    overrides = load_duration_overrides(durations_path) if durations_path else None
    beats = profile.get("music.beats")
    material = gather(
        media=media,
        fcpxml=fcpxml,
        music=music,
        overrides=overrides,
        signals=signals,
        beats=bool(beats["detect"]),
        beat_range=(float(beats["min_bpm"]), float(beats["max_bpm"])),
        fallback_bpm=beats.get("fallback_bpm"),
        beats_per_bar=int(beats["beats_per_bar"]),
    )
    adjustments = Adjustments()
    prior: dict = {}
    rounds: list[dict] = []
    warnings: list[str] = []
    reason = "max-rounds"
    result: AssemblyResult | None = None
    first: Path | None = None
    owned_router = router is None
    if router is None:
        router = Router(live=bool(live) and not dry_run_forced())
    for number in range(0, max_rounds + 1):
        taste = load_taste(taste_path)
        result = assemble(
            brief=brief,
            out_dir=destination / f"v{number}",
            style=profile,
            taste=taste,
            target_seconds=target_seconds,
            name=name,
            live=live,
            router=router,
            adjustments=adjustments,
            prior=prior,
            material=material,
            assets_dir=destination / "assets",
        )
        first = first or result.fcpxml
        for warning in result.warnings:
            if warning not in warnings:
                warnings.append(warning)
        prior = prior_index(list(result.decisions.values()) + list(result.dress_decisions.values()))
        row = _row(number, result, adjustments)
        rounds.append(row)
        if not result.failures:
            reason = "metrics"
            break
        if number == max_rounds:
            reason = "max-rounds"
            break
        adjustments, changes = refine(adjustments, result, profile)
        row["changes"] = changes
        if not changes:
            reason = "no-progress"
            break
    if owned_router:
        router.close()
    assert result is not None
    human = [f"style:{name}" for name in result.failures]
    if reason == "max-rounds" and result.failures:
        human.append("max-rounds")
    targets = result.payload["targets"]
    outcome = IterateResult(
        stop_reason=reason,
        rounds=rounds,
        applied=[],
        needs_human=bool(human),
        human_reasons=human,
        cleared=[] if result.failures else ["style"],
        warnings=warnings,
        starter=first,
        source=Path(media or fcpxml),
        mode="assemble",
        final=result.fcpxml,
    )
    payload = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "conductor_version": __version__,
        "mode": "assemble",
        "stop_reason": reason,
        "needs_human": outcome.needs_human,
        "human_reasons": human,
        "cleared": outcome.cleared,
        "brief": brief.strip(),
        "style": {**profile.to_state(), "assumptions": list(profile.get("assumptions", []) or [])},
        "target_seconds": target_seconds or parse_target(brief) or profile.get("structure.target_seconds"),
        "max_rounds": max_rounds,
        "targets": targets,
        "source": str(Path(media or fcpxml)),
        "final_fcpxml": str(result.fcpxml),
        "review_count": len(result.review),
        "applied": [],
        "rounds": rounds,
        "warnings": warnings,
    }
    out_json = destination / "iterate.json"
    out_json.write_text(dumps(payload), encoding="utf-8")
    outcome.out_json = out_json
    return outcome


def _row(number: int, result: AssemblyResult, adjustments: Adjustments) -> dict:
    metrics = result.metrics
    return {
        "round": number,
        "fcpxml": str(result.fcpxml),
        "json": str(result.json),
        "markdown": str(result.markdown),
        "mode": result.mode,
        "dtd_valid": result.dtd_errors == [] if result.dtd_errors is not None else None,
        "adjustments": adjustments.to_state(),
        "failures": list(result.failures),
        "review_count": len(result.review),
        "metrics": {
            "duration_seconds": metrics["duration_seconds"],
            "asl_by_section": metrics["asl_by_section"],
            "on_beat_ratio": metrics["on_beat"]["ratio"],
            "music_fades_ok": metrics["music"].get("compliant"),
            "ducking": [metrics["ducking"]["compliant"], metrics["ducking"]["checked"]],
            "subtitle_coverage": metrics["subtitles"]["coverage"],
            "font_compliance": metrics["fonts"]["compliance"],
            "background_coverage": metrics["background"]["coverage"],
        },
        "changes": [],
    }


def format_assembly_report(result: IterateResult) -> str:
    lines = [
        f"jevid: assemble, {len(result.rounds)} rounds (v0..v{len(result.rounds) - 1}), stop {result.stop_reason}",
        "round  length  " + "  ".join(f"asl:{k[:5]:<5}" for k in CUT_SECTIONS) + "  beat  subs   fail",
    ]
    for row in result.rounds:
        m = row["metrics"]
        asl = "  ".join(f"{m['asl_by_section'].get(k, 0.0):>9.2f}" for k in CUT_SECTIONS)
        beat = "-" if m["on_beat_ratio"] is None else f"{m['on_beat_ratio']:.2f}"
        subs = "-" if m["subtitle_coverage"] is None else f"{m['subtitle_coverage']:.2f}"
        lines.append(
            f"v{row['round']:<4}  {m['duration_seconds']:>6.2f}  {asl}  {beat:>4}  {subs:>4}   {','.join(row['failures']) or 'none'}"
        )
        for change in row.get("changes") or []:
            lines.append(f"       → {change}")
    lines.append("human  " + (", ".join(result.human_reasons) if result.human_reasons else "none"))
    if result.final is not None:
        lines.append(f"final  {result.final}")
    if result.out_json is not None:
        lines.append(f"json  {result.out_json}")
    return "\n".join(lines)

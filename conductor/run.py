"""Library entry. Read a timeline, run passes, write a shadow proposal.

``apply=True`` also writes a second FCPXML with the accepted cuts. The source
path is only ever read.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .apply import apply_edits
from .decide import deletions_for, judge
from .errors import ConductorError
from .fcpxml import Document, parse_fcpxml, write_document
from .jev import dry_run_forced
from .markers import apply_markers
from .metrics import measure
from .passes import collect, resolve_names
from .report import build_payload, dumps, render_html, render_markdown
from .router import Ledger, Router, routing_table
from .taste import Taste, feedback_event, load_taste, write_taste
from .timeutil import seconds
from .transcript import load_transcript


@dataclass
class Report:
    mode: str
    payload: dict
    marker_count: int
    cuts_applied: int = 0
    out_fcpxml: Path | None = None
    out_applied: Path | None = None
    out_json: Path | None = None
    out_markdown: Path | None = None
    out_html: Path | None = None
    out_taste: Path | None = None
    warnings: list[str] = field(default_factory=list)
    ledger: Ledger | None = None

    @property
    def candidates(self):
        return self.payload["candidates"]

    @property
    def changes(self):
        return self.payload["changes"]


def analyze(
    fcpxml_path: str | Path,
    *,
    transcript_path: str | Path | None = None,
    brief: str,
    out_dir: str | Path | None = None,
    live: bool = False,
    project: str | None = None,
    html: bool = False,
    passes: list[str] | None = None,
    taste_path: str | Path | None = None,
    apply: bool = False,
    accept: list[str] | None = None,
    min_confidence: float | None = None,
    apply_passes: list[str] | None = None,
    allow_empty_apply: bool = False,
    skip_apply: Callable[[dict], bool] | None = None,
    router: Router | None = None,
) -> Report:
    """Run the named passes and write a shadow proposal.

    ``live=False`` (the default) uses the local mock and does not read an API
    key. ``apply=True`` writes a second FCPXML. It requires ``accept`` or
    ``min_confidence`` together with ``passes`` (or ``apply_passes``).

    ``apply_passes`` limits which passes may be cut. The report still contains
    every pass in ``passes``. ``allow_empty_apply`` writes the shadow and
    skips the cut file when the gate matches nothing. ``skip_apply`` sees the
    pre-cut metrics and can decline the cut.

    ``router`` shares a decision cache and engine health across calls; its
    ``live`` wins over ``live``. Without one, a router is opened and closed
    here. The payload's ``decision_usage`` counts this call only.
    """
    source = Path(fcpxml_path)
    source_bytes = source.read_bytes()
    document = parse_fcpxml(source)
    sequences = _select(document, project)
    cues = load_transcript(transcript_path) if transcript_path else []
    taste = load_taste(taste_path)
    candidates = collect(
        sequences,
        cues,
        transcript_present=transcript_path is not None,
        requested=passes,
    )
    owned = router is None
    if router is None:
        router = Router(live=bool(live) and not dry_run_forced())
    ledger = Ledger()
    try:
        proposals, receipts = judge(
            candidates, brief, live=router.live, taste=taste, router=router, ledger=ledger
        )
    finally:
        if owned:
            router.close()
    use_live = router.live
    mode = "live" if use_live else "dry-run"
    ran = resolve_names(passes)
    by_proposal = {item.candidate_id: item for item in proposals}

    cuts: list[dict] = []
    apply_warnings: list[str] = []
    applied_doc = None
    perform_apply = apply
    if perform_apply and skip_apply is not None:
        if skip_apply(measure(sequences, proposals=proposals)):
            perform_apply = False
    if perform_apply:
        try:
            deletions = deletions_for(
                proposals,
                candidates,
                accept=accept,
                min_confidence=min_confidence,
                passes=apply_passes if apply_passes is not None else passes,
                hold=taste.hold(),
            )
        except ConductorError as exc:
            if allow_empty_apply and str(exc).startswith("no cuts matched"):
                deletions = []
            else:
                raise
        if deletions:
            applied_doc = parse_fcpxml(source)
            result = apply_edits(applied_doc, deletions)
            cuts = result.cuts
            apply_warnings = result.warnings
            by_candidate = {item.id: item for item in candidates}
            for cut in cuts:
                proposal = by_proposal[cut["candidate_id"]]
                cut["engine"] = proposal.engine
                cut["engine_source"] = proposal.engine_source
                cut["decision_type"] = proposal.decision_type
            for deletion in deletions:
                candidate = by_candidate[deletion.candidate_id]
                proposal = by_proposal[deletion.candidate_id]
                taste.append(
                    feedback_event(
                        event="accept",
                        candidate_id=deletion.candidate_id,
                        action=deletion.action,
                        pass_name=deletion.pass_name,
                        fields={
                            "clip_name": candidate.clip_name,
                            "kind": candidate.kind,
                            "timeline_start_seconds": seconds(deletion.start),
                            "timeline_end_seconds": seconds(deletion.end),
                            "engine": f"{proposal.engine}/{proposal.engine_source}",
                        },
                    )
                )

    out_fcpxml = out_applied = out_json = out_md = out_html = out_taste = None
    markers_added = 0
    files: dict[str, str | None] = {
        "fcpxml": None,
        "applied_fcpxml": None,
        "json": None,
        "markdown": None,
        "html": None,
        "taste": None,
    }
    if out_dir is not None:
        paths = output_paths(source, Path(out_dir))
        _refuse_overwrite(source, paths["fcpxml"])
        _refuse_overwrite(source, paths["applied"])
        markers_added = apply_markers(document, proposals, candidates)
        write_document(document.tree, paths["fcpxml"])
        out_fcpxml = paths["fcpxml"]
        files["fcpxml"] = str(paths["fcpxml"])
        if applied_doc is not None:
            write_document(applied_doc.tree, paths["applied"])
            out_applied = paths["applied"]
            files["applied_fcpxml"] = str(paths["applied"])
        files["json"] = str(paths["json"])
        files["markdown"] = str(paths["md"])
        files["taste"] = str(paths["taste"])
        if html:
            files["html"] = str(paths["html"])

    payload = build_payload(
        document=document,
        sequences=sequences,
        brief=brief,
        mode=mode,
        source_name=str(source),
        source_hash=hashlib.blake2b(source_bytes, digest_size=16).hexdigest(),
        transcript_name=str(transcript_path) if transcript_path else None,
        cue_count=len(cues),
        candidates=candidates,
        proposals=proposals,
        receipts=receipts,
        markers_added=markers_added,
        files=files,
        passes=ran,
        taste=taste.to_state(),
        gates=taste.gates.to_dict(),
        cuts=cuts,
        apply_warnings=apply_warnings,
        shadow=not bool(cuts),
        applied=bool(cuts),
        decision_usage=ledger.to_dict(),
        routing={
            name: row
            for name, row in routing_table().items()
            if name in {item.decision_type for item in proposals}
        },
    )
    if source.read_bytes() != source_bytes:
        raise ConductorError("refusing to finish: the source FCPXML changed during the run")
    if out_dir is not None:
        paths = output_paths(source, Path(out_dir))
        out_json = paths["json"]
        out_md = paths["md"]
        out_taste = paths["taste"]
        out_json.write_text(dumps(payload), encoding="utf-8")
        out_md.write_text(render_markdown(payload), encoding="utf-8")
        write_taste(taste, out_taste)
        if html:
            out_html = paths["html"]
            out_html.write_text(render_html(payload), encoding="utf-8")

    return Report(
        mode=mode,
        payload=payload,
        marker_count=markers_added,
        cuts_applied=len(cuts),
        out_fcpxml=out_fcpxml,
        out_applied=out_applied,
        out_json=out_json,
        out_markdown=out_md,
        out_html=out_html,
        out_taste=out_taste,
        warnings=[*ledger.warnings, *apply_warnings],
        ledger=ledger,
    )


def output_paths(source: Path, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.stem
    return {
        "fcpxml": out_dir / f"{stem}.conductor.fcpxml",
        "applied": out_dir / f"{stem}.conductor.applied.fcpxml",
        "json": out_dir / f"{stem}.conductor.json",
        "md": out_dir / f"{stem}.conductor.md",
        "html": out_dir / f"{stem}.conductor.html",
        "taste": out_dir / f"{stem}.taste.json",
    }


def _select(document: Document, project: str | None):
    if project is None:
        return list(document.sequences)
    chosen = [sequence for sequence in document.sequences if sequence.name == project]
    if not chosen:
        found = ", ".join(sequence.name for sequence in document.sequences) or "(none)"
        raise ConductorError(f"no project named {project!r}. Found: {found}")
    return chosen


def _refuse_overwrite(source: Path, dest: Path) -> None:
    if dest.resolve() == source.resolve():
        raise ConductorError(
            "refusing to overwrite the source FCPXML; choose a different --out-dir"
        )


def version() -> str:
    return __version__

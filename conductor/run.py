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
from .feedback import bind_pending, diff_fcpxml, ingest_notes
from .jev import dry_run_forced
from .markers import apply_markers
from .metrics import measure
from .passes import collect, resolve_names
from .report import build_payload, dumps, render_html, render_markdown
from .signals import gather
from .taste import Taste, feedback_event, load_taste, write_taste
from .timeutil import seconds
from .transcript import load_transcript
from .words import words_for_document, write_words


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
    signals: str = "auto",
    transcribe: str = "auto",
    signal_cache: str | Path | None = None,
    global_taste_path: str | Path | None = None,
    feedback_path: str | Path | None = None,
    learn_from: str | Path | None = None,
) -> Report:
    """Run the named passes and write a shadow proposal.

    ``live=False`` (the default) uses the local mock and does not read an API
    key. ``apply=True`` writes a second FCPXML. It requires ``accept`` or
    ``min_confidence`` together with ``passes`` (or ``apply_passes``).

    ``apply_passes`` limits which passes may be cut. The report still contains
    every pass in ``passes``. ``allow_empty_apply`` writes the shadow and
    skips the cut file when the gate matches nothing. ``skip_apply`` sees the
    pre-cut metrics and can decline the cut.

    ``learn_from`` is the previous conductor shadow FCPXML. ``fcpxml_path``
    is the editor's re-export. The diff is appended to the project taste
    before this run judges. ``feedback_path`` is a notes file from a room
    bot. ``global_taste_path`` is read-only.
    """
    source = Path(fcpxml_path)
    source_bytes = source.read_bytes()
    document = parse_fcpxml(source)
    sequences = _select(document, project)
    signal_report = gather(
        document,
        sequences,
        signals=signals,
        transcribe=transcribe,
        cache_dir=signal_cache,
        transcript_supplied=transcript_path is not None,
    )
    if transcript_path:
        cues = load_transcript(transcript_path)
        transcript_present = True
        transcript_name: str | None = str(transcript_path)
    else:
        cues = list(signal_report.cues)
        transcript_present = signal_report.transcript == "whisper"
        transcript_name = (
            f"local:{signal_report.whisper_tool or 'whisper'}"
            if transcript_present
            else None
        )
    taste = load_taste(taste_path, global_path=global_taste_path)
    learned: list[dict] = []
    learn_warnings: list[str] = []
    if learn_from:
        events, diff_warnings = diff_fcpxml(learn_from, source)
        learn_warnings.extend(diff_warnings)
        for event in events:
            if taste.append(event):
                learned.append(event)
    if feedback_path:
        noted, note_warnings = ingest_notes(taste, feedback_path)
        learn_warnings.extend(note_warnings)
        learned.extend(noted)
    candidates = collect(
        sequences,
        cues,
        transcript_present=transcript_present,
        requested=passes,
        audio_silences=signal_report.silences,
    )
    bound, bind_warnings = bind_pending(taste, candidates)
    learn_warnings.extend(bind_warnings)
    learned.extend(bound)
    use_live = bool(live) and not dry_run_forced()
    proposals, receipts = judge(candidates, brief, live=use_live, taste=taste)
    mode = "live" if use_live else "dry-run"
    ran = resolve_names(passes)

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
            for deletion in deletions:
                candidate = by_candidate[deletion.candidate_id]
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
                            "source": "person" if accept else "auto",
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
        "words": None,
        "applied_words": None,
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
        if signal_report.words:
            write_words(paths["words"], document, signal_report)
            files["words"] = str(paths["words"])
            if out_applied is not None:
                applied = parse_fcpxml(out_applied)
                applied_words = words_for_document(
                    applied, project=project, transcribe="cached", cache_dir=signal_cache
                )
                write_words(paths["applied_words"], applied, applied_words)
                files["applied_words"] = str(paths["applied_words"])

    payload = build_payload(
        document=document,
        sequences=sequences,
        brief=brief,
        mode=mode,
        source_name=str(source),
        source_hash=hashlib.blake2b(source_bytes, digest_size=16).hexdigest(),
        transcript_name=transcript_name,
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
        apply_warnings=[*learn_warnings, *apply_warnings],
        shadow=not bool(cuts),
        applied=bool(cuts),
        signals=signal_report.to_state(),
        learned=learned,
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
        warnings=[*signal_report.warnings, *learn_warnings, *apply_warnings],
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
        "words": out_dir / f"{stem}.words.json",
        "applied_words": out_dir / f"{stem}.conductor.applied.words.json",
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

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .db import make_session_factory
from .extract import process_extraction
from .indexing import discover_index_pages, extract_indexes
from .ingest import discover_sources, ingest_source
from .models import (
    Document,
    ExtractionRun,
    IndexDiscoveryRun,
    IndexEntry,
    IndexPageCandidate,
    IndexPageExtractionRun,
    OCRResult,
    Page,
    ProcessingJob,
)
from .ocr import process_ocr
from .search import search as search_database

app = typer.Typer(no_args_is_help=True, help="Index scanned TOSEC-PIX publications locally.")
logger = logging.getLogger(__name__)


def _settings() -> Settings:
    return Settings()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _document_id(session: Session, value: str | None) -> int | None:
    if value is None:
        return None
    if value.isdigit():
        document = session.get(Document, int(value))
    else:
        resolved = str(Path(value).expanduser().resolve())
        document = session.scalar(
            select(Document).where(
                or_(Document.original_path == resolved, Document.filename == value)
            )
        )
    if document is None:
        raise typer.BadParameter(f"Document not found: {value}", param_hint="--document")
    return document.id


def _job(session: Session, job_type: str, document_id: int | None) -> ProcessingJob:
    job = ProcessingJob(job_type=job_type, document_id=document_id, status="running")
    session.add(job)
    session.flush()
    return job


@app.command()
def scan(
    path: Annotated[
        Path | None, typer.Argument(help="PDF, CBZ/ZIP, image directory, or collection root")
    ] = None,
    all_categories: Annotated[
        bool,
        typer.Option("--all-categories", help="Ignore configured path keyword filters"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List matching sources without hashing or importing"),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Import at most this many matching sources"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Discover and import sources without modifying them."""
    _configure_logging(verbose)
    settings = _settings()
    roots = [path] if path else settings.input_dirs
    if not roots:
        raise typer.BadParameter("Provide PATH or configure TOSEC_INPUT_DIRS")
    sources: list[Path] = []
    keywords = [] if all_categories else settings.include_path_keywords
    for root in roots:
        remaining = None if limit is None else limit - len(sources)
        if remaining == 0:
            break
        sources.extend(discover_sources(root, keywords, remaining))
    if dry_run:
        for source in sources:
            typer.echo(source)
        typer.echo(f"{len(sources)} matching sources (nothing imported)")
        return
    imported = duplicates = failed = 0
    factory = make_session_factory(settings)
    for source in sources:
        with factory() as session:
            job = _job(session, "scan", None)
            try:
                document, created = ingest_source(session, settings, source)
                job.document_id = document.id
                job.status = "success"
                job.detail = "imported" if created else "duplicate content"
                session.commit()
                imported += int(created)
                duplicates += int(not created)
                typer.echo(
                    f"{'imported' if created else 'duplicate'} document {document.id}: {source}"
                )
            except Exception as exc:
                session.rollback()
                failed += 1
                logger.exception("Could not import %s", source)
                # Record the failure in a clean transaction.
                session.add(
                    ProcessingJob(job_type="scan", status="failed", detail=f"{source}: {exc}")
                )
                session.commit()
    typer.echo(f"scan complete: {imported} imported, {duplicates} duplicates, {failed} failed")
    if failed:
        raise typer.Exit(1)


@app.command()
def ocr(
    pending: Annotated[
        bool, typer.Option("--pending", help="Process pages without successful OCR")
    ] = False,
    document: Annotated[
        str | None, typer.Option("--document", "-d", help="Document ID, path, or filename")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Create a fresh OCR result even after success")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """OCR pending pages, preferring a useful PDF text layer."""
    _configure_logging(verbose)
    if not pending and document is None:
        raise typer.BadParameter("Use --pending or --document")
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        document_id = _document_id(session, document)
        job = _job(session, "ocr", document_id)
        summary = process_ocr(session, settings, document_id, force)
        job.status = "failed" if summary.failed else "success"
        job.detail = f"processed={summary.processed}, failed={summary.failed}"
        session.commit()
    typer.echo(f"OCR complete: {summary.processed} processed, {summary.failed} failed")
    if summary.failed:
        raise typer.Exit(1)


@app.command()
def extract(
    pending: Annotated[
        bool, typer.Option("--pending", help="Process OCR without a successful extraction")
    ] = False,
    document: Annotated[
        str | None, typer.Option("--document", "-d", help="Document ID, path, or filename")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Create a fresh extraction run")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Optional deep mode: extract broad mentions from every OCR page."""
    _configure_logging(verbose)
    if not pending and document is None:
        raise typer.BadParameter("Use --pending or --document")
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        document_id = _document_id(session, document)
        job = _job(session, "extract", document_id)
        summary = process_extraction(session, settings, document_id, force)
        job.status = "failed" if summary.failed else "success"
        job.detail = (
            f"processed={summary.processed}, failed={summary.failed}, empty={summary.skipped_empty}"
        )
        session.commit()
    typer.echo(
        f"extraction complete: {summary.processed} processed, "
        f"{summary.failed} failed, {summary.skipped_empty} empty"
    )
    if summary.failed:
        raise typer.Exit(1)


@app.command("discover-indexes")
def discover_indexes_command(
    document: Annotated[
        str, typer.Option("--document", "-d", help="Document ID, path, or filename")
    ],
    threshold: Annotated[
        float | None,
        typer.Option(help="Candidate score threshold (default comes from configuration)"),
    ] = None,
    show: Annotated[
        int, typer.Option(min=1, max=500, help="Show this many highest-scoring pages")
    ] = 20,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Score all OCR pages cheaply and persist likely contents/index pages."""
    _configure_logging(verbose)
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        document_id = _document_id(session, document)
        assert document_id is not None
        job = _job(session, "discover-indexes", document_id)
        try:
            summary = discover_index_pages(
                session,
                document_id,
                settings.index_discovery_threshold if threshold is None else threshold,
            )
            job.status = "success"
            job.detail = (
                f"run={summary.run_id}, pages={summary.pages_scored}, "
                f"selected={summary.selected}, seconds={summary.duration_seconds:.3f}"
            )
            session.commit()
        except Exception as exc:
            job.status = "failed"
            job.detail = str(exc)
            session.commit()
            raise
        rows = session.execute(
            select(IndexPageCandidate, Page)
            .join(Page, Page.id == IndexPageCandidate.page_id)
            .where(IndexPageCandidate.discovery_run_id == summary.run_id)
            .order_by(IndexPageCandidate.score.desc(), Page.page_number)
            .limit(show)
        ).all()
        typer.echo(
            f"discovery complete: {summary.pages_scored} pages scored in "
            f"{summary.duration_seconds:.3f}s; {summary.selected} selected (run {summary.run_id})"
        )
        for candidate, page in rows:
            marker = "SELECT" if candidate.selected else "      "
            reasons = "; ".join(candidate.reasons) or "no index signals"
            typer.echo(
                f"{marker} physical page {page.page_number:>3}  score={candidate.score:>4.1f}  "
                f"{candidate.classification}: {reasons}"
            )


@app.command("extract-indexes")
def extract_indexes_command(
    document: Annotated[
        str, typer.Option("--document", "-d", help="Document ID, path, or filename")
    ],
    strategy: Annotated[
        str | None,
        typer.Option(help="Candidate extractor: text or vision (default comes from configuration)"),
    ] = None,
    source_page: Annotated[
        int | None,
        typer.Option(help="Extract only this physical candidate page (for debugging)"),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Create fresh extraction runs")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Extract compact game listings from the latest selected candidate pages."""
    _configure_logging(verbose)
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        document_id = _document_id(session, document)
        assert document_id is not None
        job = _job(session, "extract-indexes", document_id)
        try:
            summary = extract_indexes(
                session,
                settings,
                document_id,
                strategy or settings.index_extraction_strategy,
                force,
                source_page,
            )
            job.status = "failed" if summary.failed else "success"
            job.detail = (
                f"processed={summary.processed}, failed={summary.failed}, "
                f"entries={summary.entries}, seconds={summary.duration_seconds:.3f}"
            )
            session.commit()
        except Exception as exc:
            job.status = "failed"
            job.detail = str(exc)
            session.commit()
            raise
    typer.echo(
        f"index extraction complete: {summary.processed} pages in "
        f"{summary.duration_seconds:.1f}s; {summary.entries} entries; {summary.failed} failed"
    )
    if summary.failed:
        raise typer.Exit(1)


@app.command("index")
def index_document(
    document: Annotated[
        str, typer.Option("--document", "-d", help="Document ID, path, or filename")
    ],
    strategy: Annotated[
        str | None,
        typer.Option(help="Candidate extractor: text or vision (default comes from configuration)"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the resumable OCR → discovery → candidate extraction workflow."""
    _configure_logging(verbose)
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        document_id = _document_id(session, document)
        assert document_id is not None
        job = _job(session, "index", document_id)
        job_id = job.id
        try:
            ocr_summary = process_ocr(session, settings, document_id)
            discovery_summary = discover_index_pages(
                session, document_id, settings.index_discovery_threshold
            )
            session.commit()
            extraction_summary = extract_indexes(
                session,
                settings,
                document_id,
                strategy or settings.index_extraction_strategy,
            )
            job = session.get(ProcessingJob, job_id)
            assert job is not None
            job.status = (
                "failed" if ocr_summary.failed or extraction_summary.failed else "success"
            )
            job.detail = (
                f"ocr={ocr_summary.processed}/{ocr_summary.failed} failed, "
                f"candidates={discovery_summary.selected}, "
                f"index_pages={extraction_summary.processed}, "
                f"entries={extraction_summary.entries}"
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ProcessingJob, job_id)
            if job is not None:
                job.status = "failed"
                job.detail = str(exc)
                session.commit()
            raise
    typer.echo(
        f"index complete: OCR {ocr_summary.processed} processed/{ocr_summary.failed} failed; "
        f"{discovery_summary.selected} candidate pages; "
        f"{extraction_summary.entries} entries from {extraction_summary.processed} pages"
    )
    if ocr_summary.failed or extraction_summary.failed:
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show durable pipeline counts."""
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        latest_discovery_ids = (
            select(func.max(IndexDiscoveryRun.id))
            .where(IndexDiscoveryRun.status == "success")
            .group_by(IndexDiscoveryRun.document_id)
        )
        current_candidate_ids = select(IndexPageCandidate.id).where(
            IndexPageCandidate.discovery_run_id.in_(latest_discovery_ids),
            IndexPageCandidate.selected.is_(True),
        )
        latest_extraction_ids = (
            select(func.max(IndexPageExtractionRun.id))
            .where(
                IndexPageExtractionRun.status == "success",
                IndexPageExtractionRun.candidate_id.in_(current_candidate_ids),
            )
            .group_by(IndexPageExtractionRun.candidate_id)
        )
        counts = {
            "documents": session.scalar(select(func.count()).select_from(Document)) or 0,
            "pages": session.scalar(select(func.count()).select_from(Page)) or 0,
            "ocr successful": session.scalar(
                select(func.count()).select_from(OCRResult).where(OCRResult.status == "success")
            )
            or 0,
            "ocr failed": session.scalar(
                select(func.count()).select_from(OCRResult).where(OCRResult.status == "failed")
            )
            or 0,
            "deep runs successful": session.scalar(
                select(func.count())
                .select_from(ExtractionRun)
                .where(ExtractionRun.status == "success")
            )
            or 0,
            "deep runs failed": session.scalar(
                select(func.count())
                .select_from(ExtractionRun)
                .where(ExtractionRun.status == "failed")
            )
            or 0,
            "index discovery runs": session.scalar(
                select(func.count()).select_from(IndexDiscoveryRun)
            )
            or 0,
            "current index candidates": session.scalar(
                select(func.count())
                .select_from(IndexPageCandidate)
                .where(IndexPageCandidate.id.in_(current_candidate_ids))
            )
            or 0,
            "current index pages": session.scalar(
                select(func.count())
                .select_from(IndexPageExtractionRun)
                .where(IndexPageExtractionRun.id.in_(latest_extraction_ids))
            )
            or 0,
            "current index entries": session.scalar(
                select(func.count())
                .select_from(IndexEntry)
                .where(IndexEntry.extraction_run_id.in_(latest_extraction_ids))
            )
            or 0,
        }
    for label, count in counts.items():
        typer.echo(f"{label:24} {count}")


@app.command("search")
def search_command(
    query: Annotated[str, typer.Argument(help="Title or text to find")],
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 50,
    include_ocr: Annotated[
        bool,
        typer.Option(
            "--include-ocr",
            help="Also search optional deep-extraction records and raw OCR",
        ),
    ] = False,
) -> None:
    """Search index listings; optionally include broad deep/OCR matches."""
    settings = _settings()
    factory = make_session_factory(settings)
    with factory() as session:
        hits = search_database(session, query, limit, include_ocr)
    for hit in hits:
        entity = hit.matched_entity or "[OCR text]"
        context = f" ({hit.context})" if hit.context else ""
        printed = (
            f" → printed page {hit.printed_page_number}"
            if hit.printed_page_number is not None
            else ""
        )
        typer.echo(
            f"{hit.filename} — source image page {hit.page_number}{printed} — {entity}{context}"
        )
        typer.echo(f"  {hit.excerpt}")
        typer.echo(f"  source: {hit.original_path}")
    if not hits:
        typer.echo("No matches.")


if __name__ == "__main__":
    app()

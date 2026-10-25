from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from .models import (
    Document,
    Entity,
    ExtractionRun,
    IndexDiscoveryRun,
    IndexEntry,
    IndexPageCandidate,
    IndexPageExtractionRun,
    Mention,
    OCRResult,
    Page,
)


@dataclass(slots=True)
class SearchHit:
    filename: str
    original_path: str
    page_number: int
    matched_entity: str | None
    excerpt: str
    context: str | None
    printed_page_number: int | None = None


def _excerpt(text: str, term: str, radius: int = 100) -> str:
    position = text.casefold().find(term.casefold())
    if position < 0:
        return text[: radius * 2].replace("\n", " ")
    start = max(0, position - radius)
    end = min(len(text), position + len(term) + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ") + suffix


def search(
    session: Session, term: str, limit: int = 50, include_ocr: bool = False
) -> list[SearchHit]:
    pattern = f"%{term}%"
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
    index_rows = session.execute(
        select(Document, Page, IndexEntry)
        .join(IndexPageCandidate, IndexPageCandidate.page_id == Page.id)
        .join(
            IndexPageExtractionRun,
            IndexPageExtractionRun.candidate_id == IndexPageCandidate.id,
        )
        .join(IndexEntry, IndexEntry.extraction_run_id == IndexPageExtractionRun.id)
        .join(Document, Document.id == Page.document_id)
        .where(
            IndexPageExtractionRun.id.in_(latest_extraction_ids),
            IndexEntry.title.ilike(pattern),
        )
        .order_by(
            case((IndexPageCandidate.classification == "contents", 1), else_=0),
            IndexPageExtractionRun.created_at.desc(),
            Document.filename,
            Page.page_number,
        )
        .limit(limit * 3)
    ).all()
    hits: list[SearchHit] = []
    seen_index_entries: set[tuple[int, str]] = set()
    for document, page, entry in index_rows:
        # Prefer a dedicated index over a general contents reference and return one
        # result per printed title per publication.
        key = (document.id, entry.normalized_title)
        if key in seen_index_entries:
            continue
        seen_index_entries.add(key)
        hits.append(
            SearchHit(
                filename=document.filename,
                original_path=document.original_path,
                page_number=page.page_number,
                matched_entity=entry.title,
                excerpt=entry.supporting_excerpt or entry.title,
                context=entry.section,
                printed_page_number=entry.printed_page_number,
            )
        )
        if len(hits) >= limit:
            break
    remaining = limit - len(hits)
    if remaining <= 0 or not include_ocr:
        return hits
    mention_rows = session.execute(
        select(Document, Page, Mention, Entity)
        .join(Page, Page.document_id == Document.id)
        .join(Mention, Mention.page_id == Page.id)
        .join(Entity, Entity.id == Mention.entity_id)
        .join(ExtractionRun, ExtractionRun.id == Mention.extraction_run_id)
        .where(
            ExtractionRun.status == "success",
            or_(Mention.printed_name.ilike(pattern), Entity.display_name.ilike(pattern)),
        )
        .order_by(Document.filename, Page.page_number)
        .limit(remaining)
    ).all()
    hits.extend(
        SearchHit(
            filename=document.filename,
            original_path=document.original_path,
            page_number=page.page_number,
            matched_entity=mention.printed_name,
            excerpt=mention.supporting_excerpt,
            context=mention.context,
        )
        for document, page, mention, _entity in mention_rows
    )
    remaining = limit - len(hits)
    if remaining <= 0:
        return hits

    ocr_rows = session.execute(
        select(Document, Page, OCRResult)
        .join(Page, Page.document_id == Document.id)
        .join(OCRResult, OCRResult.page_id == Page.id)
        .where(OCRResult.status == "success", OCRResult.text.ilike(pattern))
        .order_by(Document.filename, Page.page_number, OCRResult.created_at.desc())
        .limit(remaining * 2)
    ).all()
    mentioned_pages = {(hit.original_path, hit.page_number) for hit in hits}
    seen_ocr_pages: set[int] = set()
    for document, page, result in ocr_rows:
        if (
            page.id in seen_ocr_pages
            or (document.original_path, page.page_number) in mentioned_pages
        ):
            continue
        seen_ocr_pages.add(page.id)
        hits.append(
            SearchHit(
                filename=document.filename,
                original_path=document.original_path,
                page_number=page.page_number,
                matched_entity=None,
                excerpt=_excerpt(result.text or "", term),
                context=None,
            )
        )
        if len(hits) >= limit:
            break
    return hits

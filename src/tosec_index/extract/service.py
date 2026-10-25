from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Entity, ExtractionRun, Mention, OCRResult, Page
from .client import InvalidModelResponse, extract_page
from .schemas import PageExtraction

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExtractionSummary:
    processed: int = 0
    failed: int = 0
    skipped_empty: int = 0


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def load_prompt(settings: Settings) -> tuple[str, str]:
    prompt = settings.prompt_path.read_text(encoding="utf-8")
    fingerprint_input = prompt + json.dumps(PageExtraction.model_json_schema(), sort_keys=True)
    return prompt, hashlib.sha256(fingerprint_input.encode()).hexdigest()[:16]


def _get_or_create_entity(session: Session, entity_type: str, printed_name: str) -> Entity:
    normalized = normalize_name(printed_name)
    entity = session.scalar(
        select(Entity).where(
            Entity.entity_type == entity_type, Entity.normalized_name == normalized
        )
    )
    if entity is None:
        entity = Entity(
            entity_type=entity_type, normalized_name=normalized, display_name=printed_name.strip()
        )
        session.add(entity)
        session.flush()
    return entity


def _source_span(candidate: str, text: str) -> str | None:
    direct = re.search(re.escape(candidate), text, flags=re.IGNORECASE)
    if direct:
        return direct.group(0)
    tokens = candidate.split()
    if not tokens:
        return None
    whitespace_flexible = re.compile(
        r"\s+".join(re.escape(token) for token in tokens), flags=re.IGNORECASE
    )
    match = whitespace_flexible.search(text)
    return match.group(0) if match else None


def _validate_provenance(
    extraction: PageExtraction, page: Page, text: str
) -> tuple[list, list[str]]:
    accepted = []
    rejected: list[str] = []
    for mention in extraction.mentions:
        if mention.page_number != page.page_number:
            rejected.append(
                f"Model returned page {mention.page_number} while processing page {page.page_number}"
            )
            continue
        printed_name = _source_span(mention.printed_name, text)
        excerpt = _source_span(mention.supporting_excerpt, text)
        if printed_name is None or excerpt is None:
            rejected.append(
                "Unsupported mention discarded: "
                f"name={mention.printed_name!r}, excerpt={mention.supporting_excerpt!r}"
            )
            continue
        # Store slices copied from OCR, never the model's case/whitespace-normalized version.
        mention.printed_name = printed_name
        mention.supporting_excerpt = excerpt
        accepted.append(mention)
    return accepted, rejected


def process_extraction(
    session: Session, settings: Settings, document_id: int | None = None, force: bool = False
) -> ExtractionSummary:
    prompt, prompt_version = load_prompt(settings)
    query = (
        select(OCRResult, Page)
        .join(Page, Page.id == OCRResult.page_id)
        .where(OCRResult.status == "success")
        .order_by(Page.document_id, Page.page_number, OCRResult.created_at.desc())
    )
    if document_id is not None:
        query = query.where(Page.document_id == document_id)
    rows = session.execute(query).all()
    summary = ExtractionSummary()
    seen_pages: set[int] = set()
    for ocr_result, page in rows:
        # Only extract the newest successful OCR result for each page.
        if page.id in seen_pages:
            continue
        seen_pages.add(page.id)
        if not force:
            already_done = session.scalar(
                select(
                    exists().where(
                        ExtractionRun.ocr_result_id == ocr_result.id,
                        ExtractionRun.model == settings.llm_model,
                        ExtractionRun.model_version == settings.llm_model_version,
                        ExtractionRun.prompt_version == prompt_version,
                        ExtractionRun.status == "success",
                    )
                )
            )
            if already_done:
                continue
        text = (ocr_result.text or "").strip()
        if not text:
            summary.skipped_empty += 1
            continue

        run = ExtractionRun(
            ocr_result_id=ocr_result.id,
            model=settings.llm_model,
            model_version=settings.llm_model_version,
            prompt_version=prompt_version,
            status="running",
        )
        session.add(run)
        session.flush()
        try:
            extraction, raw = extract_page(settings, prompt, page.page_number, text)
            run.raw_response = raw
            accepted_mentions, rejected_mentions = _validate_provenance(extraction, page, text)
            if rejected_mentions:
                run.error = json.dumps(
                    {"discarded_mentions": rejected_mentions}, ensure_ascii=False
                )
            for item in accepted_mentions:
                entity = _get_or_create_entity(session, item.entity_type, item.printed_name)
                session.add(
                    Mention(
                        extraction_run_id=run.id,
                        entity_id=entity.id,
                        page_id=page.id,
                        printed_name=item.printed_name,
                        supporting_excerpt=item.supporting_excerpt,
                        context=item.context,
                        content_type=item.content_type,
                        relationship=item.relationship,
                        confidence=item.confidence,
                        uncertainty=item.uncertainty,
                    )
                )
            run.status = "success"
            run.completed_at = datetime.now(UTC)
            session.flush()
            session.commit()
            summary.processed += 1
            logger.info(
                "Extraction complete for document %s page %s", page.document_id, page.page_number
            )
        except Exception as exc:
            if isinstance(exc, InvalidModelResponse):
                run.raw_response = exc.raw_response
            run.status = "failed"
            run.error = str(exc)
            run.completed_at = datetime.now(UTC)
            session.flush()
            session.commit()
            summary.failed += 1
            logger.exception(
                "Extraction failed for document %s page %s", page.document_id, page.page_number
            )
            if isinstance(exc, httpx.HTTPError):
                logger.error("Stopping extraction batch because the model endpoint is unavailable")
                break
    return summary

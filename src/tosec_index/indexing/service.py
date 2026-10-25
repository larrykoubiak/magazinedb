from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

import httpx
import pytesseract
from PIL import Image
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..images import constrain_image
from ..models import (
    Document,
    IndexDiscoveryRun,
    IndexEntry,
    IndexPageCandidate,
    IndexPageExtractionRun,
    OCRResult,
    Page,
)
from .extractors import InvalidIndexResponse, make_extractor
from .layout import layout_lines
from .schemas import IndexPageExtraction

POSTPROCESS_VERSION = "section-filter-v1"


@dataclass(slots=True)
class IndexExtractionSummary:
    processed: int = 0
    failed: int = 0
    entries: int = 0
    duration_seconds: float = 0.0


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", value).strip()


HARDWARE_ONLY_NAMES = {
    "3do",
    "m2",
    "playstation",
    "sega saturn",
    "saturn",
}


def _prompt(settings: Settings) -> tuple[str, str]:
    prompt = settings.index_prompt_path.read_text(encoding="utf-8")
    fingerprint = "\n".join(
        (
            prompt,
            json.dumps(IndexPageExtraction.model_json_schema(), sort_keys=True),
            POSTPROCESS_VERSION,
            json.dumps(sorted(settings.index_sections)),
        )
    )
    return prompt, hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def _latest_ocr(session: Session, page_id: int) -> OCRResult | None:
    return session.scalar(
        select(OCRResult)
        .where(OCRResult.page_id == page_id, OCRResult.status == "success")
        .order_by(OCRResult.created_at.desc())
        .limit(1)
    )


def _candidate_text(page: Page, ocr: OCRResult, settings: Settings) -> str:
    visual = "\n".join(layout_lines(ocr.boxes))
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    with Image.open(Path(page.image_path)) as image:
        constrain_image(image, settings.ocr_max_image_dimension)
        focused = pytesseract.image_to_string(
            image,
            lang=settings.ocr_languages,
            config=f"--psm {settings.index_candidate_reocr_psm}",
        ).strip()
    parts = ["PRIMARY OCR:\n" + (ocr.text or "").strip()]
    if visual:
        parts.append("VISUAL-ROW RECONSTRUCTION:\n" + visual)
    if focused:
        parts.append(
            f"CANDIDATE RE-OCR (Tesseract PSM {settings.index_candidate_reocr_psm}):\n" + focused
        )
    return "\n\n".join(parts)


def _supporting_text(title: str, evidence: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"\s+".join(re.escape(part) for part in title.split()), evidence, flags=re.IGNORECASE
    )
    if match:
        return match.group(0), None
    title_key = normalize_title(title)
    candidates = [line.strip() for line in evidence.splitlines() if line.strip()]
    closest = max(
        candidates,
        key=lambda line: SequenceMatcher(None, title_key, normalize_title(line)).ratio(),
        default="",
    )
    ratio = SequenceMatcher(None, title_key, normalize_title(closest)).ratio() if closest else 0
    if ratio >= 0.55:
        return closest[:500], "title differs from OCR; verify against source image"
    return None, "title not directly supported by OCR; verify against source image"


def _evidence_page_number(title: str, evidence: str) -> int | None:
    title_key = normalize_title(title)
    best: tuple[float, int] | None = None
    for line in evidence.splitlines():
        match = re.search(r"\b(\d{1,3})\s*$", line.strip())
        if not match:
            continue
        number = int(match.group(1))
        label = normalize_title(line[: match.start()])
        similarity = SequenceMatcher(None, title_key, label).ratio()
        if title_key in label:
            similarity += 1.0
        if similarity >= 0.72 and (best is None or similarity > best[0]):
            best = (similarity, number)
    return best[1] if best else None


def _contents_section(printed_page: int | None, evidence: str) -> str:
    if printed_page is None:
        return "features"
    for line in evidence.splitlines():
        page_match = re.search(r"\b(\d{1,3})\s*$", line.strip())
        if page_match is None or int(page_match.group(1)) != printed_page:
            continue
        label = normalize_title(line)
        if re.search(r"\breviews?\b|\btests?\b", label):
            return "reviews"
        if re.search(r"\bpreviews?\b", label):
            return "previews"
        if re.search(r"\bnews\b", label):
            return "news"
    return "features"


def _superseded_contents_headlines(extraction: IndexPageExtraction, evidence: str) -> set[str]:
    """Reject a playful headline when its subtitle explicitly identifies another game."""
    titles = {
        normalize_title(item.title): item.title
        for section in extraction.sections
        for item in section.entries
    }
    lines = [line.strip() for line in evidence.splitlines() if line.strip()]
    rejected: set[str] = set()
    for index, line in enumerate(lines[:-1]):
        if not re.search(r"\b\d{1,3}\s*$", line):
            continue
        line_key = normalize_title(line)
        heading_titles = [key for key in titles if key in line_key]
        if not heading_titles:
            continue
        subtitle = " ".join(lines[index + 1 : index + 3])
        subtitle_key = normalize_title(subtitle)
        if not re.search(r"\b(video\s*game|videogame|game title)\b", subtitle_key):
            continue
        subtitle_titles = [key for key in titles if key in subtitle_key]
        if subtitle_titles:
            rejected.update(key for key in heading_titles if key not in subtitle_titles)
    return rejected


def extract_indexes(
    session: Session,
    settings: Settings,
    document_id: int,
    strategy: str = "text",
    force: bool = False,
    source_page: int | None = None,
) -> IndexExtractionSummary:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} does not exist")
    discovery = session.scalar(
        select(IndexDiscoveryRun)
        .where(
            IndexDiscoveryRun.document_id == document_id,
            IndexDiscoveryRun.status == "success",
        )
        .order_by(IndexDiscoveryRun.created_at.desc())
        .limit(1)
    )
    if discovery is None:
        raise ValueError("Run discover-indexes for this document first")
    candidate_query = (
        select(IndexPageCandidate)
        .join(Page, Page.id == IndexPageCandidate.page_id)
        .where(
            IndexPageCandidate.discovery_run_id == discovery.id,
            IndexPageCandidate.selected.is_(True),
        )
        .order_by(Page.page_number)
    )
    if source_page is not None:
        candidate_query = candidate_query.where(Page.page_number == source_page)
    candidates = list(session.scalars(candidate_query))
    prompt, prompt_version = _prompt(settings)
    extractor = make_extractor(settings, strategy)
    summary = IndexExtractionSummary()
    batch_started = time.perf_counter()
    for candidate in candidates:
        if not force:
            done = session.scalar(
                select(
                    exists().where(
                        IndexPageExtractionRun.candidate_id == candidate.id,
                        IndexPageExtractionRun.strategy == extractor.strategy,
                        IndexPageExtractionRun.model == settings.llm_model,
                        IndexPageExtractionRun.model_version == settings.llm_model_version,
                        IndexPageExtractionRun.prompt_version == prompt_version,
                        IndexPageExtractionRun.status == "success",
                    )
                )
            )
            if done:
                continue
        page = session.get(Page, candidate.page_id)
        ocr = _latest_ocr(session, candidate.page_id)
        if page is None or ocr is None:
            continue
        evidence = _candidate_text(page, ocr, settings)
        run = IndexPageExtractionRun(
            candidate_id=candidate.id,
            strategy=extractor.strategy,
            model=settings.llm_model,
            model_version=settings.llm_model_version,
            prompt_version=prompt_version,
            status="running",
            input_text=evidence or None,
        )
        session.add(run)
        session.flush()
        started = time.perf_counter()
        try:
            extraction, raw = extractor.extract(
                prompt,
                page.page_number,
                candidate.classification,
                evidence,
                Path(page.image_path),
            )
            run.raw_response = raw
            seen: set[tuple[str, int | None, str]] = set()
            rejected_headlines = (
                _superseded_contents_headlines(extraction, evidence)
                if candidate.classification == "contents"
                else set()
            )
            for section in extraction.sections:
                for item in section.entries:
                    title = re.sub(r"\s+", " ", item.title).strip()
                    if normalize_title(title) in HARDWARE_ONLY_NAMES:
                        continue
                    if normalize_title(title) in rejected_headlines:
                        continue
                    evidence_page = _evidence_page_number(title, evidence)
                    printed_page = evidence_page if evidence_page is not None else item.printed_page
                    output_section = section.section
                    if candidate.classification == "contents" and output_section == "other":
                        output_section = _contents_section(printed_page, evidence)
                    elif output_section == "other" and candidate.classification == "review_index":
                        output_section = "reviews"
                    elif output_section == "other" and candidate.classification == "preview_index":
                        output_section = "previews"
                    if output_section not in settings.index_sections:
                        continue
                    key = (normalize_title(title), printed_page, output_section)
                    if not title or key in seen:
                        continue
                    seen.add(key)
                    excerpt, provenance_warning = _supporting_text(title, evidence or raw)
                    uncertainty = provenance_warning
                    if evidence_page is not None and evidence_page != item.printed_page:
                        correction = "printed page reconciled with candidate re-OCR"
                        uncertainty = "; ".join(
                            part for part in (correction, uncertainty) if part
                        )
                    if item.uncertain:
                        uncertainty = "; ".join(
                            part for part in ("model marked uncertain", uncertainty) if part
                        )
                    session.add(
                        IndexEntry(
                            extraction_run_id=run.id,
                            section=output_section,
                            title=title,
                            normalized_title=normalize_title(title),
                            printed_page_number=printed_page,
                            supporting_excerpt=excerpt,
                            uncertainty=uncertainty,
                        )
                    )
                    summary.entries += 1
            run.status = "success"
            run.duration_seconds = time.perf_counter() - started
            run.completed_at = datetime.now(UTC)
            summary.processed += 1
            session.commit()
        except (InvalidIndexResponse, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, InvalidIndexResponse):
                run.raw_response = exc.raw_response
            run.status = "failed"
            run.error = str(exc)
            run.duration_seconds = time.perf_counter() - started
            run.completed_at = datetime.now(UTC)
            summary.failed += 1
            session.commit()
            if isinstance(exc, httpx.HTTPError):
                break
    summary.duration_seconds = time.perf_counter() - batch_started
    stored_entries = session.scalar(
        select(func.count())
        .select_from(IndexEntry)
        .join(IndexPageExtractionRun)
        .join(IndexPageCandidate)
        .where(
            IndexPageCandidate.discovery_run_id == discovery.id,
            IndexPageExtractionRun.status == "success",
        )
    ) or 0
    document = session.get(Document, document_id)
    document.index_status = "indexed_from_indexes" if stored_entries else "requires_deep_processing"
    session.flush()
    return summary

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytesseract
from PIL import Image
from pytesseract import Output
from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..images import constrain_image
from ..models import OCRResult, Page

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OCRSummary:
    processed: int = 0
    failed: int = 0
    skipped: int = 0


def pending_pages_query(document_id: int | None = None, force: bool = False) -> Select[tuple[Page]]:
    query = select(Page).order_by(Page.document_id, Page.page_number)
    if document_id is not None:
        query = query.where(Page.document_id == document_id)
    if not force:
        successful = exists().where(OCRResult.page_id == Page.id, OCRResult.status == "success")
        query = query.where(~successful)
    return query


def _pdf_text_result(page: Page, settings: Settings) -> OCRResult | None:
    text = (page.embedded_text or "").strip()
    if len(text) < settings.pdf_text_min_chars:
        return None
    return OCRResult(
        page_id=page.id,
        engine="pdf-text",
        engine_version="embedded",
        languages=settings.ocr_languages,
        status="success",
        text=text,
        mean_confidence=None,
        boxes=None,
    )


def _tesseract_result(page: Page, settings: Settings) -> OCRResult:
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    version = str(pytesseract.get_tesseract_version()).splitlines()[0]
    with Image.open(Path(page.image_path)) as image:
        scale_x, scale_y = constrain_image(image, settings.ocr_max_image_dimension)
        data = pytesseract.image_to_data(
            image, lang=settings.ocr_languages, output_type=Output.DICT
        )

    words: list[str] = []
    boxes: list[dict[str, object]] = []
    confidences: list[float] = []
    previous_line: tuple[int, int, int] | None = None
    for index, raw_text in enumerate(data["text"]):
        word = raw_text.strip()
        if not word:
            continue
        line = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        if previous_line is not None and line != previous_line:
            words.append("\n")
        elif words:
            words.append(" ")
        words.append(word)
        previous_line = line
        confidence = float(data["conf"][index])
        normalized_confidence = confidence / 100 if confidence >= 0 else None
        if normalized_confidence is not None:
            confidences.append(normalized_confidence)
        boxes.append(
            {
                "text": word,
                "confidence": normalized_confidence,
                # Store coordinates in the original cached-image coordinate space.
                "x": round(int(data["left"][index]) * scale_x),
                "y": round(int(data["top"][index]) * scale_y),
                "width": round(int(data["width"][index]) * scale_x),
                "height": round(int(data["height"][index]) * scale_y),
            }
        )
    return OCRResult(
        page_id=page.id,
        engine="tesseract",
        engine_version=version,
        languages=settings.ocr_languages,
        status="success",
        text="".join(words),
        mean_confidence=sum(confidences) / len(confidences) if confidences else None,
        boxes=boxes,
    )


def process_ocr(
    session: Session, settings: Settings, document_id: int | None = None, force: bool = False
) -> OCRSummary:
    if settings.ocr_backend != "tesseract":
        raise ValueError(f"Unsupported OCR backend: {settings.ocr_backend!r}")
    pages = list(session.scalars(pending_pages_query(document_id, force)))
    summary = OCRSummary()
    for page in pages:
        try:
            result = _pdf_text_result(page, settings) or _tesseract_result(page, settings)
            session.add(result)
            session.flush()
            session.commit()
            summary.processed += 1
            logger.info("OCR complete for document %s page %s", page.document_id, page.page_number)
        except Exception as exc:
            session.add(
                OCRResult(
                    page_id=page.id,
                    engine=settings.ocr_backend,
                    engine_version="unknown",
                    languages=settings.ocr_languages,
                    status="failed",
                    error=str(exc),
                )
            )
            session.flush()
            session.commit()
            summary.failed += 1
            logger.exception(
                "OCR failed for document %s page %s", page.document_id, page.page_number
            )
    return summary

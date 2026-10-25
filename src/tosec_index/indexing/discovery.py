from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document, IndexDiscoveryRun, IndexPageCandidate, OCRResult, Page
from .layout import layout_lines, probable_page_reference_rows

DETECTOR_VERSION = "keywords-layout-v4"

KEYWORDS = {
    "contents": ("contents", "sommaire", "sumario", "indice", "inhalt", "inhaltsverzeichnis"),
    "review_index": ("reviews", "review", "tests", "test", "essais", "recensioni", "rezensionen"),
    "preview_index": ("previews", "preview", "avant premiere", "anteprima", "vorschau"),
    "game_listing": ("game index", "games index", "game guide", "games guide", "jeux", "spiele"),
}
AD_MARKERS = ("order now", "credit card", "telephone", "call now", "dealer", "mail order")
POLL_MARKERS = ("player's poll", "players poll", "reader poll", "survey", "questionnaire")


@dataclass(slots=True)
class DiscoverySummary:
    run_id: int
    pages_scored: int
    selected: int
    duration_seconds: float


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _has_heading(text: str, phrase: str) -> bool:
    """Match a short heading, not a word embedded in ordinary article prose."""
    for raw_line in text.splitlines():
        line = _plain(raw_line).strip()
        if line == phrase or (line.startswith(f"{phrase} ") and len(line.split()) <= 3):
            return True
    return False


def _score(page: Page, ocr: OCRResult, document_pages: int) -> tuple[float, str, list[str]]:
    text = _plain(ocr.text or "")
    hits = {
        group: [phrase for phrase in phrases if _has_phrase(text, phrase)]
        for group, phrases in KEYWORDS.items()
    }
    hits["contents"] = [
        phrase for phrase in KEYWORDS["contents"] if _has_heading(ocr.text or "", phrase)
    ]
    pairs = probable_page_reference_rows(ocr.boxes, page.width, max(200, document_pages + 30))
    score = 0.0
    reasons: list[str] = []
    classification = "unknown"
    if hits["contents"]:
        score += 4.0
        classification = "contents"
        reasons.append("contents keyword: " + ", ".join(hits["contents"]))
    if hits["review_index"]:
        score += 1.5
        if classification == "unknown":
            classification = "review_index"
        reasons.append("review keyword: " + ", ".join(hits["review_index"]))
    if hits["preview_index"]:
        score += 1.5
        if classification == "unknown":
            classification = "preview_index"
        reasons.append("preview keyword: " + ", ".join(hits["preview_index"]))
    if hits["game_listing"]:
        score += 2.0
        if classification == "unknown":
            classification = "game_listing"
        reasons.append("listing keyword: " + ", ".join(hits["game_listing"]))
    structural = 6.0 if pairs >= 20 else 4.0 if pairs >= 10 else 2.5 if pairs >= 5 else 1.5 if pairs >= 3 else 0.0
    if structural:
        score += structural
        reasons.append(f"{pairs} title/page-like visual rows")
        if classification == "unknown":
            classification = "game_listing"
    if page.page_number <= 12 and (reasons or pairs):
        score += 0.5
        reasons.append("opening-page priority")
    if hits["review_index"] and hits["preview_index"] and pairs >= 5:
        classification = "contents"
        reasons.append("multiple indexed sections")
    ad_hits = [marker for marker in AD_MARKERS if marker in text]
    if ad_hits:
        score -= 3.0
        reasons.append("advertising penalty: " + ", ".join(ad_hits))
    poll_hits = [marker for marker in POLL_MARKERS if marker in text]
    if poll_hits:
        score -= 4.0
        reasons.append("reader poll penalty: " + ", ".join(poll_hits))
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line in {"letters", "mailbag", "readers letters"}:
        score -= 5.0
        reasons.append(f"reader letters penalty: {first_line}")
    return round(score, 2), classification, reasons


def discover_index_pages(
    session: Session, document_id: int, threshold: float = 4.0
) -> DiscoverySummary:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} does not exist")
    started = time.perf_counter()
    run = IndexDiscoveryRun(
        document_id=document_id,
        detector_version=DETECTOR_VERSION,
        status="running",
        threshold=threshold,
    )
    session.add(run)
    session.flush()
    rows = session.execute(
        select(Page, OCRResult)
        .join(OCRResult, OCRResult.page_id == Page.id)
        .where(Page.document_id == document_id, OCRResult.status == "success")
        .order_by(Page.page_number, OCRResult.created_at.desc())
    ).all()
    latest_rows: list[tuple[Page, OCRResult]] = []
    seen: set[int] = set()
    for page, ocr in rows:
        if page.id not in seen:
            seen.add(page.id)
            latest_rows.append((page, ocr))
    section_targets: dict[int, str] = {}
    for _page, ocr in latest_rows:
        for line in layout_lines(ocr.boxes):
            match = re.search(
                r"\b(reviews?|tests?|previews?)\b.*?\b(\d{1,3})\s*$",
                _plain(line),
            )
            if match:
                label = "preview_index" if match.group(1).startswith("preview") else "review_index"
                section_targets[int(match.group(2))] = label
    seen.clear()
    selected = 0
    for page, ocr in latest_rows:
        seen.add(page.id)
        score, classification, reasons = _score(page, ocr, document.page_count)
        if classification == "game_listing" and page.page_number in section_targets:
            classification = section_targets[page.page_number]
            reasons.append("section linked from a contents page")
        is_selected = score >= threshold
        selected += int(is_selected)
        session.add(
            IndexPageCandidate(
                discovery_run_id=run.id,
                page_id=page.id,
                classification=classification,
                score=score,
                selected=is_selected,
                reasons=reasons,
            )
        )
    duration = time.perf_counter() - started
    run.status = "success"
    run.duration_seconds = duration
    run.completed_at = datetime.now(UTC)
    document.index_status = "index_candidates_found" if selected else "requires_deep_processing"
    session.flush()
    return DiscoverySummary(run.id, len(seen), selected, duration)

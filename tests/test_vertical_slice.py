from __future__ import annotations

import zipfile
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tosec_index.config import Settings
from tosec_index.extract.schemas import ExtractedMention, PageExtraction
from tosec_index.extract.service import _source_span, process_extraction
from tosec_index.indexing.discovery import discover_index_pages
from tosec_index.ingest import discover_sources, ingest_source
from tosec_index.models import Base, Document, IndexPageCandidate, Mention, OCRResult, Page
from tosec_index.ocr import process_ocr
from tosec_index.search import search


def test_pdf_to_search_vertical_slice(tmp_path: Path, monkeypatch) -> None:
    import pymupdf

    pdf_path = tmp_path / "Amiga Power (1992) (Issue 10).pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Dungeon Master is the primary game in this review.")
    pdf.save(pdf_path)
    pdf.close()

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        cache_dir=tmp_path / "cache",
        pdf_text_min_chars=10,
        prompt_path=Path(__file__).parents[1] / "prompts" / "extract_mentions.txt",
    )
    with Session(engine) as session:
        document, created = ingest_source(session, settings, pdf_path)
        session.commit()
        assert created is True
        assert document.page_count == 1
        assert document.parsed_metadata["year_guess"] == 1992

        ocr_summary = process_ocr(session, settings, document.id)
        session.commit()
        assert ocr_summary.processed == 1
        result = session.scalar(select(OCRResult))
        assert result is not None and result.engine == "pdf-text"

        def fake_extract(_settings, _prompt, page_number, text):
            extraction = PageExtraction(
                mentions=[
                    ExtractedMention(
                        printed_name="Dungeon Master",
                        entity_type="software",
                        page_number=page_number,
                        supporting_excerpt="Dungeon Master is the primary game",
                        context="primary_subject",
                        content_type="review",
                        confidence=0.98,
                    )
                ]
            )
            return extraction, extraction.model_dump_json()

        monkeypatch.setattr("tosec_index.extract.service.extract_page", fake_extract)
        extraction_summary = process_extraction(session, settings, document.id)
        session.commit()
        assert extraction_summary.processed == 1
        assert session.scalar(select(Mention)) is not None

        assert search(session, "Dungeon Master") == []
        hits = search(session, "Dungeon Master", include_ocr=True)
        assert len(hits) == 1
        assert hits[0].page_number == 1
        assert hits[0].matched_entity == "Dungeon Master"
        assert hits[0].context == "primary_subject"


def test_cbz_pages_are_natural_sorted_and_content_deduplicated(tmp_path: Path) -> None:
    image_2 = tmp_path / "page2.png"
    image_10 = tmp_path / "page10.png"
    Image.new("RGB", (20, 10), "red").save(image_2)
    Image.new("RGB", (30, 10), "blue").save(image_10)
    archive = tmp_path / "Magazine.cbz"
    with zipfile.ZipFile(archive, "w") as cbz:
        cbz.write(image_10, "scans/page10.png")
        cbz.write(image_2, "scans/page2.png")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(cache_dir=tmp_path / "cache")
    with Session(engine) as session:
        document, created = ingest_source(session, settings, archive)
        session.commit()
        assert created is True
        pages = list(
            session.scalars(
                select(Page).where(Page.document_id == document.id).order_by(Page.page_number)
            )
        )
        assert [(page.width, page.height) for page in pages] == [(20, 10), (30, 10)]

        duplicate = tmp_path / "copy.cbz"
        duplicate.write_bytes(archive.read_bytes())
        same_document, created = ingest_source(session, settings, duplicate)
        assert created is False
        assert same_document.id == document.id
        assert str(duplicate.resolve()) in same_document.parsed_metadata["source_paths"]
        assert session.scalar(select(Document).where(Document.id == document.id)) is not None


def test_discovery_filters_on_parent_category_names(tmp_path: Path) -> None:
    books = tmp_path / "System" / "Books"
    artwork = tmp_path / "System" / "Artwork"
    manual_pages = tmp_path / "Other" / "Manuals" / "Hardware Guide"
    books.mkdir(parents=True)
    artwork.mkdir(parents=True)
    manual_pages.mkdir(parents=True)
    (books / "Programming.zip").write_bytes(b"not opened during discovery")
    (artwork / "Covers.zip").write_bytes(b"not opened during discovery")
    Image.new("RGB", (10, 10), "white").save(manual_pages / "001.png")

    sources = discover_sources(tmp_path, ["magazine", "book", "catalog", "manual"])

    assert books / "Programming.zip" in sources
    assert manual_pages in sources
    assert artwork / "Covers.zip" not in sources
    assert len(discover_sources(tmp_path, ["book", "manual"], limit=1)) == 1


def test_zip_containing_pdf_is_streamed_and_ingested(tmp_path: Path) -> None:
    import pymupdf

    embedded_pdf = tmp_path / "issue.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=2500, height=3500)
    page.insert_text((72, 72), "A searchable magazine page")
    pdf.save(embedded_pdf)
    pdf.close()
    archive = tmp_path / "issue.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.write(embedded_pdf, "nested/issue.pdf")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(cache_dir=tmp_path / "cache", page_image_max_dimension=1000)
    with Session(engine) as session:
        document, created = ingest_source(session, settings, archive)
        session.commit()
        page = session.scalar(select(Page).where(Page.document_id == document.id))

    assert created is True
    assert document.page_count == 1
    assert page is not None and page.embedded_text == "A searchable magazine page"
    assert max(page.width or 0, page.height or 0) <= 1000
    assert not list((tmp_path / "cache").rglob(".embedded-*.pdf"))


def test_model_excerpt_is_mapped_back_to_exact_ocr_text() -> None:
    ocr = "ELECTRONIC\nARTS Presents: Dungeon Master"

    assert _source_span("Electronic Arts presents", ocr) == "ELECTRONIC\nARTS Presents"
    assert _source_span("Dungeon Master", ocr) == "Dungeon Master"
    assert _source_span("Dungeon Master II", ocr) is None


def test_index_discovery_prefers_contents_and_dense_page_lists(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        document = Document(
            sha256="a" * 64,
            original_path=str(tmp_path / "issue.cbz"),
            filename="issue.cbz",
            source_type="cbz",
            page_count=5,
            parsed_metadata={},
        )
        session.add(document)
        session.flush()
        pages = []
        texts = (
            "CONTENTS Reviews",
            "Reviews",
            "Games",
            "letters\nI have been pleased with its contents and reviews",
            "PLAYER'S POLL\nOf the game reviews below, which two did you enjoy the most?",
        )
        for number, text in enumerate(texts, 1):
            page = Page(
                document_id=document.id,
                page_number=number,
                image_path=str(tmp_path / f"{number}.jpg"),
                image_sha256=str(number) * 64,
                width=1000,
                height=1400,
            )
            session.add(page)
            session.flush()
            boxes = []
            if number in (1, 2, 3, 4, 5):
                row_count = 3 if number == 2 else 12 if number in (3, 4, 5) else 5
                for row in range(row_count):
                    boxes.extend(
                        [
                            {"text": f"Game{row}", "x": 200, "y": 100 + row * 30, "width": 90, "height": 20},
                            {"text": str(20 + row), "x": 800, "y": 100 + row * 30, "width": 25, "height": 20},
                        ]
                    )
            session.add(
                OCRResult(
                    page_id=page.id,
                    engine="test",
                    engine_version="1",
                    languages="eng",
                    status="success",
                    text=text,
                    boxes=boxes,
                )
            )
            pages.append(page)
        session.flush()
        summary = discover_index_pages(session, document.id, threshold=4.0)
        candidates = list(
            session.scalars(
                select(IndexPageCandidate)
                .where(IndexPageCandidate.discovery_run_id == summary.run_id)
                .order_by(IndexPageCandidate.page_id)
            )
        )

    assert summary.selected == 3
    assert [candidate.selected for candidate in candidates] == [True, True, True, False, False]
    assert candidates[0].classification == "contents"
    assert candidates[1].classification == "contents"
    assert "continuation of preceding contents page" in candidates[1].reasons
    assert candidates[2].classification == "game_listing"
    assert "reader poll penalty: player's poll" in candidates[4].reasons

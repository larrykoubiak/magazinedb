from __future__ import annotations

import hashlib
import re
import shutil
import zipfile
from collections.abc import Iterable
from pathlib import Path

import pymupdf
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Document, Page

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
ARCHIVE_SUFFIXES = {".cbz", ".zip"}
SUPPORTED_FILE_SUFFIXES = {".pdf", *ARCHIVE_SUFFIXES}


def natural_key(value: str) -> list[str | int]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_directory(path: Path, images: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for image in images:
        digest.update(image.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(hash_file(image)))
    return digest.hexdigest()


def parse_filename_metadata(path: Path) -> dict[str, object]:
    stem = path.stem
    groups = re.findall(r"\(([^()]*)\)", stem)
    title = re.split(r"\s+\(", stem, maxsplit=1)[0].strip()
    metadata: dict[str, object] = {"title_guess": title or stem}
    if groups:
        metadata["filename_tags"] = groups
    issue = re.search(r"(?:issue|no\.?|#)\s*(\d+)", stem, re.IGNORECASE)
    if issue:
        metadata["issue_guess"] = issue.group(1)
    year = re.search(r"\b(19\d{2}|20\d{2})\b", stem)
    if year:
        metadata["year_guess"] = int(year.group(1))
    return metadata


def _image_files(path: Path) -> list[Path]:
    return sorted(
        (
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES
        ),
        key=lambda item: natural_key(item.name),
    )


def _path_matches(path: Path, keywords: tuple[str, ...]) -> bool:
    if not keywords:
        return True
    return any(keyword in part.casefold() for part in path.parts for keyword in keywords)


def discover_sources(
    path: Path, include_path_keywords: Iterable[str] = (), limit: int | None = None
) -> list[Path]:
    path = path.expanduser().resolve()
    keywords = tuple(
        keyword.strip().casefold() for keyword in include_path_keywords if keyword.strip()
    )
    if path.is_file():
        if path.suffix.casefold() not in SUPPORTED_FILE_SUFFIXES:
            raise ValueError(f"Unsupported source format: {path}")
        return [path] if _path_matches(path, keywords) else []
    if not path.is_dir():
        raise FileNotFoundError(path)

    found: list[Path] = []
    for directory, dirs, files in path.walk():
        dirs.sort(key=natural_key)
        files.sort(key=natural_key)
        if _path_matches(directory, keywords) and any(
            Path(name).suffix.casefold() in IMAGE_SUFFIXES for name in files
        ):
            found.append(directory)
            if limit is not None and len(found) >= limit:
                return found
        for name in files:
            candidate = directory / name
            if candidate.suffix.casefold() in SUPPORTED_FILE_SUFFIXES and _path_matches(
                candidate, keywords
            ):
                found.append(candidate)
                if limit is not None and len(found) >= limit:
                    return found
    return found


def _save_image(
    source: Image.Image, destination: Path, max_dimension: int
) -> tuple[int, int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rgb = source.convert("RGB")
    if max_dimension > 0 and max(rgb.size) > max_dimension:
        rgb.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    rgb.save(destination, "JPEG", quality=90)
    return rgb.width, rgb.height, hash_file(destination)


def _extract_pdf(
    path: Path,
    cache: Path,
    dpi: int,
    max_dimension: int,
    start_page: int = 1,
) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    with pymupdf.open(path) as pdf:
        for index, pdf_page in enumerate(pdf):
            destination = cache / f"{start_page + index:04d}.jpg"
            if not destination.exists():
                zoom = dpi / 72
                rendered_long_edge = max(pdf_page.rect.width, pdf_page.rect.height) * zoom
                if max_dimension > 0 and rendered_long_edge > max_dimension:
                    zoom *= max_dimension / rendered_long_edge
                pixmap = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                pixmap.save(destination)
            with Image.open(destination) as image:
                width, height = image.size
            pages.append(
                {
                    "image_path": str(destination.resolve()),
                    "image_sha256": hash_file(destination),
                    "width": width,
                    "height": height,
                    "embedded_text": pdf_page.get_text("text").strip() or None,
                }
            )
    return pages


def _extract_archive(
    path: Path, cache: Path, dpi: int, max_dimension: int
) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    with zipfile.ZipFile(path) as archive:
        image_names = sorted(
            (
                info.filename
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).suffix.casefold() in IMAGE_SUFFIXES
            ),
            key=natural_key,
        )
        pdf_names = sorted(
            (
                info.filename
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).suffix.casefold() == ".pdf"
            ),
            key=natural_key,
        )
        if image_names and pdf_names:
            raise ValueError("Archive mixes page images and PDFs; page ordering is ambiguous")
        for index, name in enumerate(image_names, start=1):
            destination = cache / f"{index:04d}.jpg"
            if not destination.exists():
                # Read the member as a stream; never trust or extract its archive path.
                with archive.open(name) as raw, Image.open(raw) as image:
                    _save_image(image, destination, max_dimension)
            with Image.open(destination) as image:
                width, height = image.size
            pages.append(
                {
                    "image_path": str(destination.resolve()),
                    "image_sha256": hash_file(destination),
                    "width": width,
                    "height": height,
                    "embedded_text": None,
                }
            )
        for pdf_index, name in enumerate(pdf_names, start=1):
            embedded_pdf = cache / f".embedded-{pdf_index}.pdf"
            try:
                with archive.open(name) as raw, embedded_pdf.open("wb") as destination:
                    shutil.copyfileobj(raw, destination, length=1024 * 1024)
                pages.extend(
                    _extract_pdf(
                        embedded_pdf,
                        cache,
                        dpi,
                        max_dimension,
                        start_page=len(pages) + 1,
                    )
                )
            finally:
                embedded_pdf.unlink(missing_ok=True)
    return pages


def _read_image_directory(path: Path) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    for image_path in _image_files(path):
        with Image.open(image_path) as image:
            width, height = image.size
        pages.append(
            {
                "image_path": str(image_path.resolve()),
                "image_sha256": hash_file(image_path),
                "width": width,
                "height": height,
                "embedded_text": None,
            }
        )
    return pages


def ingest_source(session: Session, settings: Settings, source: Path) -> tuple[Document, bool]:
    source = source.expanduser().resolve()
    if source.is_dir():
        image_files = _image_files(source)
        if not image_files:
            raise ValueError(f"Directory contains no supported page images: {source}")
        source_type = "image_directory"
        sha256 = hash_directory(source, image_files)
    elif source.suffix.casefold() == ".pdf":
        source_type = "pdf"
        sha256 = hash_file(source)
    elif source.suffix.casefold() in ARCHIVE_SUFFIXES:
        source_type = "cbz" if source.suffix.casefold() == ".cbz" else "zip"
        sha256 = hash_file(source)
    else:
        raise ValueError(f"Unsupported source format: {source}")

    existing = session.scalar(select(Document).where(Document.sha256 == sha256))
    if existing:
        metadata = dict(existing.parsed_metadata or {})
        source_paths = list(metadata.get("source_paths", [existing.original_path]))
        source_path = str(source)
        if source_path not in source_paths:
            source_paths.append(source_path)
            metadata["source_paths"] = source_paths
            existing.parsed_metadata = metadata
            session.flush()
        return existing, False

    settings.ensure_cache_dirs()
    cache = (settings.cache_dir / "pages" / sha256).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    try:
        if source_type == "pdf":
            page_data = _extract_pdf(
                source,
                cache,
                settings.pdf_dpi,
                settings.page_image_max_dimension,
            )
        elif source_type in {"cbz", "zip"}:
            page_data = _extract_archive(
                source,
                cache,
                settings.pdf_dpi,
                settings.page_image_max_dimension,
            )
        else:
            page_data = _read_image_directory(source)
        if not page_data:
            raise ValueError(f"Source contains no pages: {source}")

        metadata = parse_filename_metadata(source)
        metadata["source_paths"] = [str(source)]
        document = Document(
            sha256=sha256,
            original_path=str(source),
            filename=source.name,
            source_type=source_type,
            page_count=len(page_data),
            parsed_metadata=metadata,
        )
        session.add(document)
        session.flush()
        session.add_all(
            Page(document_id=document.id, page_number=index, **data)
            for index, data in enumerate(page_data, start=1)
        )
        session.flush()
        return document, True
    except Exception:
        if source_type != "image_directory":
            shutil.rmtree(cache, ignore_errors=True)
        raise

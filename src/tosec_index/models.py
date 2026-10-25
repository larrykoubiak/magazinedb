from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import relationship as orm_relationship

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32))
    page_count: Mapped[int] = mapped_column(Integer)
    parsed_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    index_status: Mapped[str] = mapped_column(String(32), default="not_indexed", index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    pages: Mapped[list[Page]] = orm_relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    image_path: Mapped[str] = mapped_column(Text)
    image_sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedded_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[Document] = orm_relationship(back_populates="pages")
    ocr_results: Mapped[list[OCRResult]] = orm_relationship(
        back_populates="page", cascade="all, delete-orphan"
    )


class OCRResult(Base):
    __tablename__ = "ocr_results"
    __table_args__ = (
        Index("ix_ocr_page_config", "page_id", "engine", "engine_version", "languages"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    engine: Mapped[str] = mapped_column(String(64))
    engine_version: Mapped[str] = mapped_column(String(128))
    languages: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    mean_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    boxes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    page: Mapped[Page] = orm_relationship(back_populates="ocr_results")
    extraction_runs: Mapped[list[ExtractionRun]] = orm_relationship(back_populates="ocr_result")


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    ocr_result_id: Mapped[int] = mapped_column(
        ForeignKey("ocr_results.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ocr_result: Mapped[OCRResult] = orm_relationship(back_populates="extraction_runs")
    mentions: Mapped[list[Mention]] = orm_relationship(
        back_populates="extraction_run", cascade="all, delete-orphan"
    )


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("entity_type", "normalized_name"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    normalized_name: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    mentions: Mapped[list[Mention]] = orm_relationship(back_populates="entity")


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    printed_name: Mapped[str] = mapped_column(Text)
    supporting_excerpt: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(32))
    relationship: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_run: Mapped[ExtractionRun] = orm_relationship(back_populates="mentions")
    entity: Mapped[Entity] = orm_relationship(back_populates="mentions")
    page: Mapped[Page] = orm_relationship()


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32), index=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IndexDiscoveryRun(Base):
    __tablename__ = "index_discovery_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    detector_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    threshold: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidates: Mapped[list[IndexPageCandidate]] = orm_relationship(
        back_populates="discovery_run", cascade="all, delete-orphan"
    )


class IndexPageCandidate(Base):
    __tablename__ = "index_page_candidates"
    __table_args__ = (UniqueConstraint("discovery_run_id", "page_id"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    discovery_run_id: Mapped[int] = mapped_column(
        ForeignKey("index_discovery_runs.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True
    )
    classification: Mapped[str] = mapped_column(String(32))
    score: Mapped[float] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(default=False, index=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    discovery_run: Mapped[IndexDiscoveryRun] = orm_relationship(back_populates="candidates")
    page: Mapped[Page] = orm_relationship()
    extraction_runs: Mapped[list[IndexPageExtractionRun]] = orm_relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class IndexPageExtractionRun(Base):
    __tablename__ = "index_page_extraction_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("index_page_candidates.id", ondelete="CASCADE"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidate: Mapped[IndexPageCandidate] = orm_relationship(back_populates="extraction_runs")
    entries: Mapped[list[IndexEntry]] = orm_relationship(
        back_populates="extraction_run", cascade="all, delete-orphan"
    )


class IndexEntry(Base):
    __tablename__ = "index_entries"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("index_page_extraction_runs.id", ondelete="CASCADE"), index=True
    )
    section: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text, index=True)
    printed_page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    physical_target_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), nullable=True
    )
    supporting_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_run: Mapped[IndexPageExtractionRun] = orm_relationship(back_populates="entries")
    physical_target_page: Mapped[Page | None] = orm_relationship(
        foreign_keys=[physical_target_page_id]
    )

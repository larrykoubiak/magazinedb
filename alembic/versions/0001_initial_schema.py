"""Initial provenance-first indexing schema."""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("original_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("parsed_metadata", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_sha256", "documents", ["sha256"], unique=True)
    op.create_table(
        "pages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=False),
        sa.Column("image_sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("embedded_text", sa.Text()),
        sa.UniqueConstraint("document_id", "page_number"),
    )
    op.create_index("ix_pages_document_id", "pages", ["document_id"])
    op.create_table(
        "ocr_results",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "page_id",
            sa.BigInteger(),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("engine", sa.String(64), nullable=False),
        sa.Column("engine_version", sa.String(128), nullable=False),
        sa.Column("languages", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("mean_confidence", sa.Float()),
        sa.Column("boxes", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ocr_results_page_id", "ocr_results", ["page_id"])
    op.create_index("ix_ocr_results_status", "ocr_results", ["status"])
    op.create_index(
        "ix_ocr_page_config", "ocr_results", ["page_id", "engine", "engine_version", "languages"]
    )
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "ocr_result_id",
            sa.BigInteger(),
            sa.ForeignKey("ocr_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("raw_response", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_extraction_runs_ocr_result_id", "extraction_runs", ["ocr_result_id"])
    op.create_index("ix_extraction_runs_status", "extraction_runs", ["status"])
    op.create_table(
        "entities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.UniqueConstraint("entity_type", "normalized_name"),
    )
    op.create_index("ix_entities_entity_type", "entities", ["entity_type"])
    op.create_table(
        "mentions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "extraction_run_id",
            sa.BigInteger(),
            sa.ForeignKey("extraction_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_id", sa.BigInteger(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column(
            "page_id",
            sa.BigInteger(),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("printed_name", sa.Text(), nullable=False),
        sa.Column("supporting_excerpt", sa.Text(), nullable=False),
        sa.Column("context", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("relationship", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.Column("uncertainty", sa.Text()),
    )
    op.create_index("ix_mentions_extraction_run_id", "mentions", ["extraction_run_id"])
    op.create_index("ix_mentions_entity_id", "mentions", ["entity_id"])
    op.create_index("ix_mentions_page_id", "mentions", ["page_id"])
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column(
            "document_id", sa.BigInteger(), sa.ForeignKey("documents.id", ondelete="CASCADE")
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_processing_jobs_job_type", "processing_jobs", ["job_type"])
    op.create_index("ix_processing_jobs_status", "processing_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_table("mentions")
    op.drop_table("entities")
    op.drop_table("extraction_runs")
    op.drop_table("ocr_results")
    op.drop_table("pages")
    op.drop_table("documents")

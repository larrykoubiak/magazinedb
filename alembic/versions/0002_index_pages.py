"""Add lightweight index discovery and extraction records."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("index_status", sa.String(32), nullable=False, server_default="not_indexed"),
    )
    op.create_index("ix_documents_index_status", "documents", ["index_status"])
    op.create_table(
        "index_discovery_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("document_id", sa.BigInteger(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("detector_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_index_discovery_runs_document_id", "index_discovery_runs", ["document_id"])
    op.create_index("ix_index_discovery_runs_status", "index_discovery_runs", ["status"])
    op.create_table(
        "index_page_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("discovery_run_id", sa.BigInteger(), sa.ForeignKey("index_discovery_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.BigInteger(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("discovery_run_id", "page_id"),
    )
    op.create_index("ix_index_page_candidates_discovery_run_id", "index_page_candidates", ["discovery_run_id"])
    op.create_index("ix_index_page_candidates_page_id", "index_page_candidates", ["page_id"])
    op.create_index("ix_index_page_candidates_selected", "index_page_candidates", ["selected"])
    op.create_table(
        "index_page_extraction_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("candidate_id", sa.BigInteger(), sa.ForeignKey("index_page_candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_text", sa.Text()),
        sa.Column("raw_response", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_index_page_extraction_runs_candidate_id", "index_page_extraction_runs", ["candidate_id"])
    op.create_index("ix_index_page_extraction_runs_status", "index_page_extraction_runs", ["status"])
    op.create_table(
        "index_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("extraction_run_id", sa.BigInteger(), sa.ForeignKey("index_page_extraction_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("printed_page_number", sa.Integer()),
        sa.Column("physical_target_page_id", sa.BigInteger(), sa.ForeignKey("pages.id", ondelete="SET NULL")),
        sa.Column("supporting_excerpt", sa.Text()),
        sa.Column("uncertainty", sa.Text()),
    )
    op.create_index("ix_index_entries_extraction_run_id", "index_entries", ["extraction_run_id"])
    op.create_index("ix_index_entries_section", "index_entries", ["section"])
    op.create_index("ix_index_entries_normalized_title", "index_entries", ["normalized_title"])


def downgrade() -> None:
    op.drop_table("index_entries")
    op.drop_table("index_page_extraction_runs")
    op.drop_table("index_page_candidates")
    op.drop_table("index_discovery_runs")
    op.drop_index("ix_documents_index_status", table_name="documents")
    op.drop_column("documents", "index_status")

"""brain: document links

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_links",
        sa.Column("from_document_id", sa.UUID(), nullable=False),
        sa.Column("to_document_id", sa.UUID(), nullable=True),
        sa.Column("relation", sa.String(length=30), nullable=False),
        sa.Column("ref_number", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("label", sa.String(length=300), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["from_document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_document_id", "relation", "ref_number", name="uq_document_link"),
    )
    op.create_index("ix_document_links_from_document_id", "document_links", ["from_document_id"])
    op.create_index("ix_document_links_to_document_id", "document_links", ["to_document_id"])
    op.create_index("ix_document_links_organization_id", "document_links", ["organization_id"])


def downgrade() -> None:
    op.drop_table("document_links")

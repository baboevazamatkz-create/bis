"""Three library levels, versioned folder sync and project-bound conversation memory."""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    from bis.db import Base
    from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion

    for model in (LibraryConnection, LibraryFile, LibraryVersion):
        Base.metadata.tables[model.__tablename__].create(op.get_bind(), checkfirst=True)
    op.create_foreign_key("fk_library_active_version", "library_files", "library_versions", ["active_version_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_knowledge_sources_document_id", "knowledge_sources", ["document_id"])
    for table in ("chat_sessions", "org_memory"):
        op.add_column(table, sa.Column("object_id", sa.UUID(), sa.ForeignKey("construction_objects.id", ondelete="CASCADE")))
        op.create_index(f"ix_{table}_object_id", table, ["object_id"])
    op.execute("""UPDATE org_memory m SET object_id = d.object_id FROM documents d
        WHERE m.source = 'document:' || d.id::text AND m.organization_id = d.organization_id""")
    op.execute("""UPDATE org_memory SET is_active = false WHERE object_id IS NULL
        AND (source LIKE 'document:%' OR source = 'demo' OR kind IN ('fact', 'example', 'feedback', 'alias'))""")


def downgrade():
    op.drop_constraint("fk_library_active_version", "library_files", type_="foreignkey")
    op.drop_index("ix_knowledge_sources_document_id", "knowledge_sources")
    for table in ("org_memory", "chat_sessions"):
        op.drop_index(f"ix_{table}_object_id", table)
        op.drop_column(table, "object_id")
    for table in ("library_versions", "library_files", "library_connections"):
        op.drop_table(table)

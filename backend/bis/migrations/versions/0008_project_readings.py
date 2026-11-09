"""Incremental project sheet readings and specification provenance."""
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    from bis.db import Base
    from bis.models.project_reading import ProjectReading
    Base.metadata.tables[ProjectReading.__tablename__].create(op.get_bind())


def downgrade():
    op.drop_table('project_readings')

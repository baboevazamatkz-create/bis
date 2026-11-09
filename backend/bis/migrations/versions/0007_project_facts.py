"""Project fact ledger, conflicts and investigations."""
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    from bis.db import Base
    from bis.models.facts import BrainInvestigation, FactConflict, FactDecision, FactSnapshot, ProjectFact
    for model in (FactSnapshot, ProjectFact, FactConflict, FactDecision, BrainInvestigation):
        Base.metadata.tables[model.__tablename__].create(op.get_bind())


def downgrade():
    for name in ('brain_investigations', 'fact_decisions', 'fact_conflicts', 'project_facts', 'fact_snapshots'):
        op.drop_table(name)

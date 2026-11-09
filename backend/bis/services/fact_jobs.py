"""Incrementally warm project facts for existing archives and newly connected folders."""
from types import SimpleNamespace

from sqlalchemy import select

from bis.jobs import job
from bis.models import ConstructionObject
from bis.services.project_facts import refresh_project


@job('project_facts', 'Синапс: реестр фактов проектов', every_minutes=1)
def refresh(db, org, today):
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    projects = db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == org.id,
                                                           ConstructionObject.status == 'active')).all()
    result = {}
    for project in projects:
        db.info.pop('source_context', None)
        ctx = SimpleNamespace(db=db, org_id=org.id, user=None, gateway=get_gateway(), storage=get_storage(),
                              embedder=get_embedder(), focus={'object': str(project.id)}, progress=None)
        try:
            result[str(project.id)] = refresh_project(ctx, project.id, limit=10, checkpoint=db.commit)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — continue with other isolated projects
            db.rollback()
            result[str(project.id)] = {'error': type(exc).__name__}
    db.info.pop('source_context', None)
    return result

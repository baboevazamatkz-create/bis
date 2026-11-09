"""Read large project files incrementally, without one long interactive request."""
import datetime as dt
from types import SimpleNamespace

from sqlalchemy import select

from bis.jobs import job
from bis.models import ConstructionObject, Document
from bis.services.library_access import SourceContext, document_predicate
from bis.services.project_facts import SourceInProgress
from bis.services.project_reading import current, read


@job('project_reading', 'Синапс: постраничное чтение проектов', every_minutes=1)
def refresh(db, org, today):
    from bis.llm.gateway import get_gateway
    from bis.services.storage import get_storage
    done, failed = 0, 0
    projects = db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == org.id,
        ConstructionObject.status == 'active')).all()
    for project in projects:
        db.info.pop('source_context', None)
        ctx = SimpleNamespace(db=db, org_id=org.id, gateway=get_gateway(), storage=get_storage(), focus={'object': str(project.id)})
        docs = db.scalars(select(Document).where(document_predicate(SourceContext(org.id, project.id, True)),
            Document.object_id == project.id, Document.kind == 'project_doc', Document.status.notin_(['cancelled', 'replaced', 'rejected']))
            .order_by(Document.updated_at, Document.id)).all()
        pending = 0
        for doc in docs:
            try:
                cached = current(ctx, doc.id)
                if cached and not cached.payload['coverage']['remaining']:
                    continue
                if pending >= 5:
                    break
                pending += 1
                read(ctx, doc.id, budget=4)
                doc.data = {**(doc.data or {}), 'project_reading_attempt_at': dt.datetime.now(dt.UTC).isoformat()}
                db.commit()
                done += 1
            except Exception as exc:  # noqa: BLE001 — isolate unread files and move attempted failures behind untouched documents
                db.rollback()
                if isinstance(exc, SourceInProgress):
                    failed += 1
                    continue
                failed_doc = db.get(Document, doc.id)
                if failed_doc and failed_doc.organization_id == org.id and failed_doc.object_id == project.id:
                    failed_doc.data = {**(failed_doc.data or {}), 'project_reading_attempt_at': dt.datetime.now(dt.UTC).isoformat(),
                        'project_reading_error': type(exc).__name__}
                    db.commit()
                failed += 1
        db.info.pop('source_context', None)
    return {'documents': done, 'failed': failed}

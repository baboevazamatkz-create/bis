"""Use the existing persistent scheduler; sync shared folders once per cycle."""
from sqlalchemy import select

from bis.jobs import job
from bis.models import Organization
from bis.models.libraries import LibraryConnection
from bis.services.library_sync import execute


@job('libraries', 'Синапс: синхронизация библиотек', every_minutes=1)
def synchronize(db, org, today):
    first = db.scalar(select(Organization.id).order_by(Organization.id).limit(1))
    ids = db.scalars(select(LibraryConnection.id).where(LibraryConnection.enabled.is_(True),
        (LibraryConnection.organization_id == org.id) | (LibraryConnection.organization_id.is_(None) if org.id == first else False))).all()
    db.commit()
    for connection_id in ids:
        execute(connection_id)
    return {'connections': len(ids)}

"""Recover persisted executive jobs after an API restart; fence abandoned attempts."""

import datetime as dt

from sqlalchemy import select

from bis.jobs import job
from bis.models import IntakeEvent


@job("executive", "Синапс: восстановление заданий ИД", every_minutes=1)
def recover(db, org, today):
    from bis.api.executive import execute

    events = db.scalars(select(IntakeEvent).where(IntakeEvent.organization_id == org.id,
        IntakeEvent.result["pipeline"].astext == "1", IntakeEvent.result["status"].astext.in_(["queued", "running"]))
        .order_by(IntakeEvent.created_at).limit(10).with_for_update(skip_locked=True)).all()
    queued, abandoned = [], 0
    for event in events:
        if event.result["status"] == "queued":
            queued.append((event.id, event.user_id))
        else:
            started = dt.datetime.fromisoformat(event.result.get("heartbeat_at") or event.result.get("started_at") or event.created_at.isoformat())
            if dt.datetime.now(dt.UTC) - started > dt.timedelta(minutes=30):
                event.result = {**event.result, "status": "error", "attempt": None,
                                "error": "Обработка прервана или превысила 30 минут. Данные сохранены; повторите проверку."}
                abandoned += 1
    db.commit()
    for run_id, user_id in queued:
        execute(run_id, user_id)
    return {"resumed": len(queued), "abandoned": abandoned}

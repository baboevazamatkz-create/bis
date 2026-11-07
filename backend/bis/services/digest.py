"""Сводка дня и контроль ежедневных отчётов (как «Daily Log» у Procore и Raken, но из того, что ИИ уже разобрал).

Вечером руководителю: что сделано (объёмы), что пришло (материалы), замечания, что ждёт решения, сроки, кто из прорабов
не отчитался. Днём — напоминание прорабам, которые ещё ничего не прислали. Без модели: только факты из базы.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from bis.config import get_settings
from bis.jobs import job
from bis.models import (
    AIAction,
    ConstructionObject,
    EstimateItem,
    IntakeEvent,
    Material,
    MaterialReceipt,
    Organization,
    Task,
    User,
    WorkVolume,
)
from bis.services.notify import managers, notify


def _num(v) -> str:
    return f"{float(v):,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def _day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min).astimezone()  # местное время сервера
    return start, start + dt.timedelta(days=1)


def build(db: Session, org_id: uuid.UUID, day: dt.date) -> dict[str, Any]:
    start, end = _day_bounds(day)
    objects = {o.id: o.code or o.name for o in db.scalars(select(ConstructionObject).where(
        ConstructionObject.organization_id == org_id))}

    done = []
    for v, it in db.execute(select(WorkVolume, EstimateItem).join(EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id)
                            .where(WorkVolume.organization_id == org_id, WorkVolume.created_at >= start,
                                   WorkVolume.created_at < end)
                            .order_by(WorkVolume.created_at)).all():
        done.append({"work": it.name, "quantity": float(v.quantity), "unit": it.unit, "location": v.location,
                     "by": v.reported_by, "status": v.status, "object": objects.get(v.object_id),
                     "date": v.date.isoformat() if v.date != day else None})
    received = []
    for r, m in db.execute(select(MaterialReceipt, Material).join(Material, Material.id == MaterialReceipt.material_id)
                           .where(MaterialReceipt.organization_id == org_id, MaterialReceipt.created_at >= start,
                                  MaterialReceipt.created_at < end)).all():
        received.append({"material": m.name, "quantity": float(r.quantity), "unit": m.unit, "doc": r.document_ref,
                         "quality_doc": r.quality_doc, "object": objects.get(r.object_id)})
    # замечания контролёра и проблемы из отчётов с площадки (простои, недопоставки)
    issues = [{"title": a.title.removeprefix("Проблема на объекте: "), "severity": a.severity, "summary": a.summary}
              for a in db.scalars(select(AIAction).where(
                  AIAction.organization_id == org_id, AIAction.status != "rejected",
                  AIAction.created_at >= start, AIAction.created_at < end,
                  or_(and_(AIAction.kind == "flag_issue", AIAction.severity != "info"),
                      and_(AIAction.kind == "create_task", AIAction.title.like("Проблема на объекте:%"))))
                  .order_by(AIAction.created_at))]
    pending = db.scalar(select(func.count()).select_from(AIAction).where(AIAction.organization_id == org_id,
                                                                         AIAction.status == "proposed")) or 0
    open_tasks = (Task.organization_id == org_id, Task.status.in_(["open", "in_progress"]), Task.due_date.is_not(None))
    overdue = db.scalar(select(func.count()).select_from(Task).where(*open_tasks, Task.due_date < day)) or 0
    tomorrow = db.scalar(select(func.count()).select_from(Task).where(*open_tasks,
                                                                     Task.due_date == day + dt.timedelta(days=1))) or 0
    events = db.scalar(select(func.count()).select_from(IntakeEvent).where(
        IntakeEvent.organization_id == org_id, IntakeEvent.created_at >= start, IntakeEvent.created_at < end)) or 0
    return {"date": day.isoformat(), "done": done, "received": received, "issues": issues, "pending": pending,
            "overdue_tasks": overdue, "tasks_tomorrow": tomorrow, "events": events,
            "silent_foremen": [u.full_name for u in silent_foremen(db, org_id, day)]}


def silent_foremen(db: Session, org_id: uuid.UUID, day: dt.date) -> list[User]:
    """Прорабы, от которых за день ничего не пришло (ни фото, ни голосового, ни текста)."""
    start, end = _day_bounds(day)
    reported = select(IntakeEvent.user_id).where(IntakeEvent.organization_id == org_id, IntakeEvent.user_id.is_not(None),
                                                 IntakeEvent.created_at >= start, IntakeEvent.created_at < end)
    return list(db.scalars(select(User).where(User.organization_id == org_id, User.role == "foreman",
                                              User.is_active.is_(True), User.id.notin_(reported))
                           .order_by(User.full_name)))


def text(d: dict[str, Any]) -> str:
    day = dt.date.fromisoformat(d["date"])
    lines = [f"Сводка за {day:%d.%m.%Y}"]
    if d["done"]:
        lines.append("Сделано:")
        lines += [f"• {x['work'][:90]} — {_num(x['quantity'])} {x['unit']}"
                  + (f", {x['location']}" if x["location"] else "") + (f" ({x['by']})" if x["by"] else "")
                  + (f" (за {dt.date.fromisoformat(x['date']):%d.%m})" if x.get("date") else "")
                  + ("" if x["status"] != "reported" else " — ждёт подтверждения ПТО") for x in d["done"][:12]]
    if d["received"]:
        lines.append("Пришло:")
        lines += [f"• {x['material'][:80]} — {_num(x['quantity'])} {x['unit']}" + (f", {x['doc']}" if x["doc"] else "")
                  + ("" if x["quality_doc"] else " — нет документа о качестве") for x in d["received"][:12]]
    if d["issues"]:
        lines.append(f"Замечания ({len(d['issues'])}):")
        lines += [f"• {'‼' if x['severity'] == 'critical' else '⚠'} {x['title'][:140]}" for x in d["issues"][:8]]
    if not (d["done"] or d["received"] or d["issues"]):
        lines.append("С объектов за день ничего не пришло." if not d["events"] else f"Сообщений с объектов: {d['events']}.")
    tail = []
    if d["pending"]:
        tail.append(f"ждут решения: {d['pending']}")
    if d["overdue_tasks"]:
        tail.append(f"просрочено поручений: {d['overdue_tasks']}")
    if d["tasks_tomorrow"]:
        tail.append(f"срок завтра: {d['tasks_tomorrow']}")
    if tail:
        lines.append("Внимание: " + ", ".join(tail) + ".")
    if d["silent_foremen"]:
        lines.append("Не отчитались: " + ", ".join(d["silent_foremen"]) + ".")
    return "\n".join(lines)


REMIND = ("Отчёт за день: что сделано (работа, объём, оси/захватка), что пришло, что мешает — пришлите сюда "
          "голосом, текстом или фото. ИИ разнесёт сам.")


@job("daily_digest", "Сводка дня руководителю и напоминание прорабам", every_minutes=20)
def daily_digest(db: Session, org: Organization, today: dt.date) -> dict[str, Any]:
    """Днём (с BIS_REMIND_HOUR) — напоминание прорабам без отчёта; вечером (с BIS_DIGEST_HOUR) — сводка руководителям.
    Повторно в тот же день не отправляется (ключ уведомления)."""
    s = get_settings()
    hour = dt.datetime.now().hour  # местное время компьютера/сервера BIS
    stats = {"reminded": 0, "digest": 0}
    if today.weekday() == 6 and not s.digest_sunday:
        return stats
    if s.remind_hour <= hour < s.digest_hour:
        for u in silent_foremen(db, org.id, today):
            stats["reminded"] += notify(db, org.id, [u], title="Не забудьте отчёт за день", body=REMIND,
                                        key=f"remind:{today}")
    if hour >= s.digest_hour:
        d = build(db, org.id, today)
        if d["events"] or d["done"] or d["received"] or d["issues"] or d["silent_foremen"]:
            body = text(d).split("\n", 1)[1]
            stats["digest"] += notify(db, org.id, managers(db, org.id), title=f"Сводка за {today:%d.%m.%Y}", body=body,
                                      entity_type="digest", entity_id=today.isoformat(), key=f"digest:{today}")
    return stats

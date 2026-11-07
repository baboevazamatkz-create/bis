"""Фоновые задачи: контроль сроков, почта, план-факт.

Запуск: `bis worker` (постоянный цикл) или `bis run-jobs [имя ...]` (из cron / по требованию).
Каждая задача обходит все организации и пишет итог в журнал job_runs.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.models import Contract, ContractObligation, Counterparty, JobRun, Organization, Task, User
from bis.services.notify import managers, notify, users_for_person

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    title: str
    every_minutes: int
    func: Callable[[Session, Organization, date], dict[str, Any]]


JOBS: dict[str, Job] = {}


def job(name: str, title: str, every_minutes: int):
    def wrap(func):
        JOBS[name] = Job(name, title, every_minutes, func)
        return func

    return wrap


CRITICAL_AFTER_DAYS = 7


@job("deadlines", "Контроль сроков обязательств и поручений", every_minutes=60)
def check_deadlines(db: Session, org: Organization, today: date) -> dict[str, Any]:
    stats = {"reminders": 0, "overdue": 0, "critical": 0}
    bosses = managers(db, org.id)
    admins = managers(db, org.id, ("admin",))

    obligations = db.scalars(select(ContractObligation).where(
        ContractObligation.organization_id == org.id,
        ContractObligation.status.in_(["open", "overdue"]),
        ContractObligation.due_date.is_not(None),
    ))
    for ob in obligations:
        contract = db.get(Contract, ob.contract_id)
        cp = db.get(Counterparty, contract.counterparty_id) if contract and contract.counterparty_id else None
        label = f"договор № {contract.number}" if contract else "договор"
        responsible = users_for_person(db, ob.responsible_id or (contract.responsible_id if contract else None))
        days = (ob.due_date - today).days
        who = "Наше обязательство" if ob.party == "we" else f"Обязательство контрагента{f' ({cp.name})' if cp else ''}"
        text = f"{who}: {ob.description}\nСрок: {ob.due_date:%d.%m.%Y}, {label}" + (
            f", {ob.source_clause}" if ob.source_clause else "")
        if 0 <= days <= ob.notify_days_before:
            when = "сегодня" if days == 0 else f"через {days} дн."
            stats["reminders"] += notify(
                db, org.id, responsible or bosses, title=f"Срок {when}: {ob.description[:120]}", body=text,
                level="warning" if days <= 1 else "info", entity_type="obligation", entity_id=ob.id,
                key=f"obl:{ob.id}:soon:{ob.due_date}")
        elif days < 0:
            ob.status = "overdue"
            hint = ("" if ob.party == "we" else
                    "\nАгент «Договорной» может рассчитать неустойку и подготовить претензию.")
            stats["overdue"] += notify(
                db, org.id, responsible + bosses, title=f"Просрочено ({-days} дн.): {ob.description[:120]}",
                body=text + hint, level="warning", entity_type="obligation", entity_id=ob.id,
                key=f"obl:{ob.id}:overdue:{ob.due_date}")
            if -days >= CRITICAL_AFTER_DAYS and ob.party == "we":
                stats["critical"] += notify(
                    db, org.id, admins, title=f"Критическая просрочка {-days} дн.: {ob.description[:100]}",
                    body=text + (f"\nНеустойка: {ob.penalty}" if ob.penalty else ""), level="critical",
                    entity_type="obligation", entity_id=ob.id, key=f"obl:{ob.id}:critical:{ob.due_date}")

    tasks = db.scalars(select(Task).where(Task.organization_id == org.id, Task.status.in_(["open", "in_progress"]),
                                          Task.due_date.is_not(None)))
    for t in tasks:
        days = (t.due_date - today).days
        assignees = users_for_person(db, t.assignee_person_id)
        if t.assignee_user_id:
            user = db.get(User, t.assignee_user_id)
            assignees += [user] if user else []
        if 0 <= days <= 2:
            stats["reminders"] += notify(
                db, org.id, assignees or bosses, title=f"Поручение: срок {t.due_date:%d.%m.%Y}", body=t.title,
                level="info", entity_type="task", entity_id=t.id, key=f"task:{t.id}:soon:{t.due_date}")
        elif days < 0:
            stats["overdue"] += notify(
                db, org.id, assignees + bosses, title=f"Поручение просрочено на {-days} дн.", body=t.title,
                level="warning", entity_type="task", entity_id=t.id, key=f"task:{t.id}:overdue:{t.due_date}")
    return stats


def run_job(db_factory: Callable[[], Session], name: str, today: date | None = None) -> JobRun:
    j = JOBS[name]
    today = today or date.today()
    with db_factory() as db:
        run = JobRun(name=name, status="running")
        db.add(run)
        db.commit()
        run_id = run.id
    details: dict[str, Any] = {}
    status = "ok"
    with db_factory() as db:
        for org in db.scalars(select(Organization)).all():
            try:
                details[str(org.id)] = j.func(db, org, today)
                db.commit()
            except Exception as e:  # noqa: BLE001 — сбой одной организации не останавливает остальные
                db.rollback()
                status = "error"
                details[str(org.id)] = {"error": str(e), "trace": traceback.format_exc()[-2000:]}
                log.exception("Задача %s, организация %s", name, org.id)
    with db_factory() as db:
        run = db.get(JobRun, run_id)
        run.status, run.details, run.finished_at = status, details, datetime.now(UTC)
        db.commit()
        db.refresh(run)
        return run


def due_jobs(db: Session, now: datetime) -> list[str]:
    due = []
    for name, j in JOBS.items():
        last = db.scalar(select(JobRun.started_at).where(JobRun.name == name).order_by(JobRun.started_at.desc()).limit(1))
        if last is None or now - last >= timedelta(minutes=j.every_minutes):
            due.append(name)
    return due


def worker(db_factory: Callable[[], Session], poll_seconds: int = 60, once: bool = False) -> None:
    log.info("Воркер BIS запущен, задачи: %s", ", ".join(JOBS))
    while True:
        with db_factory() as db:
            names = due_jobs(db, datetime.now(UTC))
        for name in names:
            run_job(db_factory, name)
        if once:
            return
        time.sleep(poll_seconds)


def load_all_jobs() -> dict[str, Job]:
    """Импортирует модули, регистрирующие свои фоновые задачи."""
    import bis.connectors.mailbox  # noqa: F401

    return JOBS

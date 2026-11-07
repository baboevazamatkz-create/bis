"""Инструменты договорной работы и контроля исполнения: договоры, обязательства, неустойки, поручения."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from bis.agents.base import AgentContext, tool
from bis.agents.builtin_tools import _uuid
from bis.models import Contract, ContractObligation, Counterparty, Person, Task, User
from bis.services import contracts as csvc
from bis.services.audit import audit


def _person_name(ctx: AgentContext, person_id) -> str | None:
    p = ctx.db.get(Person, person_id) if person_id else None
    return p.full_name if p else None


def _obligation_out(ctx: AgentContext, o: ContractObligation) -> dict:
    today = dt.date.today()
    return {"id": str(o.id), "contract_id": str(o.contract_id), "description": o.description, "party": o.party,
            "kind": o.kind, "due_date": o.due_date, "due_text": o.due_text, "status": o.status,
            "days_left": (o.due_date - today).days if o.due_date else None, "penalty": o.penalty,
            "clause": o.source_clause, "responsible": _person_name(ctx, o.responsible_id), "amount": o.amount}


class FindPersonsArgs(BaseModel):
    query: str | None = Field(None, description="ФИО, должность")
    limit: int = Field(20, ge=1, le=100)


@tool("find_persons", "Поиск ответственных лиц (сотрудники и представители контрагентов).", scopes=("registry:read",))
def find_persons(ctx: AgentContext, args: FindPersonsArgs) -> dict:
    stmt = select(Person).where(Person.organization_id == ctx.org_id)
    if args.query:
        q = f"%{args.query}%"
        stmt = stmt.where(or_(Person.full_name.ilike(q), Person.position.ilike(q)))
    rows = ctx.db.scalars(stmt.limit(args.limit)).all()
    return {"persons": [{"id": str(p.id), "full_name": p.full_name, "position": p.position, "nrs_id": p.nrs_id,
                         "organization": (ctx.db.get(Counterparty, p.counterparty_id).name if p.counterparty_id else "наша")}
                        for p in rows]}


class ContractArgs(BaseModel):
    contract_id: str


@tool("get_contract_details", "Карточка договора: стороны, суммы, сроки, результаты анализа, обязательства.",
      scopes=("registry:read",))
def get_contract_details(ctx: AgentContext, args: ContractArgs) -> dict:
    c = ctx.db.get(Contract, _uuid(args.contract_id, "договора"))
    if c is None or c.organization_id != ctx.org_id:
        raise LookupError("Договор не найден")
    cp = ctx.db.get(Counterparty, c.counterparty_id) if c.counterparty_id else None
    obligations = ctx.db.scalars(select(ContractObligation).where(ContractObligation.contract_id == c.id)
                                 .order_by(ContractObligation.due_date.nulls_last())).all()
    return {"id": str(c.id), "kind": c.kind, "number": c.number, "date": c.date, "subject": c.subject,
            "our_role": c.our_role, "counterparty": cp.name if cp else None, "amount": c.amount, "vat_rate": c.vat_rate,
            "start_date": c.start_date, "end_date": c.end_date, "status": c.status,
            "responsible": _person_name(ctx, c.responsible_id), "has_file": c.document_id is not None,
            "analysis": (c.extra or {}).get("analysis"), "obligations": [_obligation_out(ctx, o) for o in obligations]}


@tool("analyze_contract", "Проанализировать текст договора: извлечь обязательства, сроки, неустойки, риски и "
      "добавить обязательства в реестр контроля.", scopes=("registry:write",))
def analyze_contract(ctx: AgentContext, args: ContractArgs) -> dict:
    c = ctx.db.get(Contract, _uuid(args.contract_id, "договора"))
    if c is None or c.organization_id != ctx.org_id:
        raise LookupError("Договор не найден")
    data, result, added = csvc.analyze_contract(ctx.db, ctx.storage, ctx.gateway, c, actor=f"agent:{ctx.agent_name}")
    return {"obligations_found": len(data.obligations), "obligations_added": added, "risks": data.risks,
            "penalties": data.penalties[:10], "advance": data.advance, "warranty": data.warranty,
            "method": "правила (модель недоступна)" if result.used_fallback else f"модель {result.provider}"}


class ListObligationsArgs(BaseModel):
    contract_id: str | None = None
    status: Literal["open", "overdue", "done", "cancelled"] | None = None
    party: Literal["we", "counterparty"] | None = None
    due_within_days: int | None = Field(None, description="Только со сроком в ближайшие N дней")
    overdue_only: bool = False
    limit: int = Field(50, ge=1, le=200)


@tool("list_obligations", "Реестр обязательств по договорам с фильтрами (просроченные, ближайшие сроки, сторона).",
      scopes=("registry:read",))
def list_obligations(ctx: AgentContext, args: ListObligationsArgs) -> dict:
    today = dt.date.today()
    stmt = select(ContractObligation).where(ContractObligation.organization_id == ctx.org_id)
    if args.contract_id:
        stmt = stmt.where(ContractObligation.contract_id == _uuid(args.contract_id, "договора"))
    if args.status:
        stmt = stmt.where(ContractObligation.status == args.status)
    if args.party:
        stmt = stmt.where(ContractObligation.party == args.party)
    if args.overdue_only:
        stmt = stmt.where(ContractObligation.due_date < today, ContractObligation.status.in_(["open", "overdue"]))
    if args.due_within_days is not None:
        stmt = stmt.where(ContractObligation.due_date.between(today, today + dt.timedelta(days=args.due_within_days)),
                          ContractObligation.status == "open")
    rows = ctx.db.scalars(stmt.order_by(ContractObligation.due_date.nulls_last()).limit(args.limit)).all()
    return {"obligations": [_obligation_out(ctx, o) for o in rows]}


class UpdateObligationArgs(BaseModel):
    obligation_id: str
    status: Literal["open", "done", "cancelled"] | None = None
    due_date: dt.date | None = None
    responsible_id: str | None = None


@tool("update_obligation", "Изменить статус, срок или ответственного по обязательству.", scopes=("registry:write",))
def update_obligation(ctx: AgentContext, args: UpdateObligationArgs) -> dict:
    o = ctx.db.get(ContractObligation, _uuid(args.obligation_id, "обязательства"))
    if o is None or o.organization_id != ctx.org_id:
        raise LookupError("Обязательство не найдено")
    if args.status:
        o.status = args.status
    if args.due_date:
        o.due_date = args.due_date
    if args.responsible_id:
        p = ctx.db.get(Person, _uuid(args.responsible_id, "ответственного"))
        if p is None or p.organization_id != ctx.org_id:
            raise LookupError("Ответственный не найден")
        o.responsible_id = p.id
    audit(ctx.db, ctx.org_id, actor_type="agent", actor=ctx.agent_name, action="obligation.update",
          entity_type="obligation", entity_id=o.id, details=args.model_dump(mode="json", exclude_none=True))
    return _obligation_out(ctx, o)


class PenaltyArgs(BaseModel):
    base_amount: float = Field(description="Сумма, от которой считается неустойка (просроченный платёж/цена работ)")
    days: int = Field(description="Дней просрочки")
    rate_percent_per_day: float | None = Field(None, description="Ставка в % за каждый день (например 0.1)")
    key_rate_percent: float | None = Field(None, description="Ключевая ставка ЦБ, % годовых")
    key_rate_fraction: str | None = Field(None, description="Доля ключевой ставки, например 1/300")
    cap_percent: float | None = Field(None, description="Ограничение неустойки, % от суммы")


@tool("calc_penalty", "Точный расчёт неустойки (пени) за просрочку. Ставку и ограничение бери из договора.")
def calc_penalty_tool(ctx: AgentContext, args: PenaltyArgs) -> dict:
    return csvc.calc_penalty(**args.model_dump()).model_dump()


# --- поручения ----------------------------------------------------------------------------------


def _task_out(ctx: AgentContext, t: Task) -> dict:
    user = ctx.db.get(User, t.assignee_user_id) if t.assignee_user_id else None
    return {"id": str(t.id), "title": t.title, "description": t.description, "status": t.status,
            "priority": t.priority, "due_date": t.due_date,
            "assignee": _person_name(ctx, t.assignee_person_id) or (user.full_name if user else None),
            "overdue": bool(t.due_date and t.due_date < dt.date.today() and t.status in ("open", "in_progress")),
            "source": t.source_type}


class ListTasksArgs(BaseModel):
    status: Literal["open", "in_progress", "done", "cancelled"] | None = None
    assignee_query: str | None = Field(None, description="Часть ФИО исполнителя")
    overdue_only: bool = False
    mine: bool = Field(False, description="Только поручения текущего пользователя")
    limit: int = Field(50, ge=1, le=200)


@tool("list_tasks", "Список поручений с фильтрами: статус, исполнитель, просроченные.", scopes=("registry:read",))
def list_tasks(ctx: AgentContext, args: ListTasksArgs) -> dict:
    stmt = select(Task).where(Task.organization_id == ctx.org_id)
    if args.status:
        stmt = stmt.where(Task.status == args.status)
    if args.overdue_only:
        stmt = stmt.where(Task.due_date < dt.date.today(), Task.status.in_(["open", "in_progress"]))
    if args.mine:
        stmt = stmt.where(Task.assignee_user_id == ctx.user.id)
    if args.assignee_query:
        ids = ctx.db.scalars(select(Person.id).where(Person.organization_id == ctx.org_id,
                                                     Person.full_name.ilike(f"%{args.assignee_query}%"))).all()
        stmt = stmt.where(Task.assignee_person_id.in_(ids))
    rows = ctx.db.scalars(stmt.order_by(Task.due_date.nulls_last()).limit(args.limit)).all()
    return {"tasks": [_task_out(ctx, t) for t in rows]}


class CreateTaskArgs(BaseModel):
    title: str
    description: str | None = None
    assignee_person_id: str | None = None
    due_date: dt.date | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    object_id: str | None = None
    contract_id: str | None = None
    source_type: str | None = Field(None, description="letter, obligation, meeting, agent")
    source_id: str | None = None


@tool("create_task", "Создать поручение с исполнителем и сроком.", scopes=("documents:write",))
def create_task(ctx: AgentContext, args: CreateTaskArgs) -> dict:
    person = None
    if args.assignee_person_id:
        person = ctx.db.get(Person, _uuid(args.assignee_person_id, "исполнителя"))
        if person is None or person.organization_id != ctx.org_id:
            raise LookupError("Исполнитель не найден")
    t = Task(organization_id=ctx.org_id, title=args.title, description=args.description,
             assignee_person_id=person.id if person else None, assignee_user_id=person.user_id if person else None,
             due_date=args.due_date, priority=args.priority,
             object_id=_uuid(args.object_id, "объекта") if args.object_id else None,
             contract_id=_uuid(args.contract_id, "договора") if args.contract_id else None,
             source_type=args.source_type or "agent", source_id=args.source_id, created_by=f"agent:{ctx.agent_name}")
    ctx.db.add(t)
    ctx.db.flush()
    audit(ctx.db, ctx.org_id, actor_type="agent", actor=ctx.agent_name, action="task.create", entity_type="task",
          entity_id=t.id, details={"title": args.title, "user": str(ctx.user.id)})
    if person:
        from bis.services.notify import notify, users_for_person

        notify(ctx.db, ctx.org_id, users_for_person(ctx.db, person.id), title="Новое поручение", body=t.title,
               entity_type="task", entity_id=str(t.id), key=f"task:{t.id}:new")
    return _task_out(ctx, t)


class UpdateTaskArgs(BaseModel):
    task_id: str
    status: Literal["open", "in_progress", "done", "cancelled"] | None = None
    due_date: dt.date | None = None
    result: str | None = Field(None, description="Отчёт об исполнении")


@tool("update_task", "Изменить статус, срок поручения или записать отчёт об исполнении.", scopes=("documents:write",))
def update_task(ctx: AgentContext, args: UpdateTaskArgs) -> dict:
    t = ctx.db.get(Task, _uuid(args.task_id, "поручения"))
    if t is None or t.organization_id != ctx.org_id:
        raise LookupError("Поручение не найдено")
    if args.status:
        t.status = args.status
        if args.status == "done":
            t.completed_at = dt.datetime.now(dt.UTC)
    if args.due_date:
        t.due_date = args.due_date
    if args.result:
        t.result = args.result
    audit(ctx.db, ctx.org_id, actor_type="agent", actor=ctx.agent_name, action="task.update", entity_type="task",
          entity_id=t.id, details=args.model_dump(mode="json", exclude_none=True))
    return _task_out(ctx, t)


class DocumentArgs(BaseModel):
    document_id: str


def _owned_document(ctx: AgentContext, document_id: str):
    from bis.models import Document

    doc = ctx.db.get(Document, _uuid(document_id, "документа"))
    if doc is None or doc.organization_id != ctx.org_id:
        raise LookupError("Документ не найден")
    return doc


@tool("get_document", "Документ из реестра: реквизиты, результаты разбора и текст (для ответа на письмо, проверки).",
      scopes=("documents:read",))
def get_document(ctx: AgentContext, args: DocumentArgs) -> dict:
    from bis.rag.parsers import UnsupportedFormat, parse_file

    doc = _owned_document(ctx, args.document_id)
    text = None
    if doc.file_key:
        try:
            text = parse_file(ctx.storage.get(doc.file_key), doc.file_name or "file", doc.mime).text[:8000]
        except UnsupportedFormat:
            text = None
    return {"id": str(doc.id), "kind": doc.kind, "title": doc.title, "number": doc.number, "date": doc.date,
            "status": doc.status, "analysis": (doc.data or {}).get("analysis"), "text": text}


@tool("process_incoming_letter", "Разобрать входящее письмо: зарегистрировать, определить суть и срок ответа, "
      "создать поручения ответственным.", scopes=("documents:write",))
def process_incoming_letter(ctx: AgentContext, args: DocumentArgs) -> dict:
    from bis.services.correspondence import process_incoming

    doc = _owned_document(ctx, args.document_id)
    a = process_incoming(ctx.db, ctx.storage, ctx.gateway, doc, actor=f"agent:{ctx.agent_name}")
    tasks = [_task_out(ctx, ctx.db.get(Task, _uuid(t, "поручения"))) for t in (doc.data or {}).get("tasks", [])]
    return {"registered_as": doc.number, "analysis": a.model_dump(mode="json"), "tasks": tasks}


CONTROL_TOOLS = [get_document, process_incoming_letter, find_persons, get_contract_details, analyze_contract, list_obligations, update_obligation,
                 calc_penalty_tool, list_tasks, create_task, update_task]


class DelegateArgs(BaseModel):
    agent: str = Field(description="Имя агента: norms, secretary, pto, contracts, control, works, materials, closing, "
                       "cad, project")
    task: str = Field(description="Самодостаточное задание для агента со всеми нужными данными")


@tool("delegate_to_agent", "Передать часть задачи профильному агенту и получить результат (для составных задач).")
def delegate_to_agent(ctx: AgentContext, args: DelegateArgs) -> dict:
    if ctx.depth >= 1:
        raise ValueError("Повторное делегирование запрещено — выполни задачу сам")
    if not ctx.agents or args.agent not in ctx.agents:
        raise LookupError(f"Агент «{args.agent}» не найден")
    caller = ctx.agent_name
    sub = ctx.agents[args.agent]
    if sub.name == caller:
        raise ValueError("Нельзя делегировать самому себе")
    ctx.depth += 1
    try:
        result = sub.run(ctx, ctx.tool_registry, [], args.task)
    finally:
        ctx.depth -= 1
        ctx.agent_name = caller
    return {"agent": sub.name, "title": sub.title, "answer": result.text,
            "documents": [d for d in result.documents]}


CONTROL_TOOLS.append(delegate_to_agent)

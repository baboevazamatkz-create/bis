"""Инструменты исполнительной документации: АООК, реестр ИД, журнал выполнения работ."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

from bis.agents.base import AgentContext, tool
from bis.agents.builtin_tools import AOSRArgs, _doc_ref
from bis.agents.resolve import OBJ_HINT, obj_row
from bis.documents.aook import AOOKData
from bis.models import ConstructionObject
from bis.services import documents as docsvc
from bis.services import exec_docs as idsvc


def _object(ctx: AgentContext, object_id: str | None) -> ConstructionObject:
    return obj_row(ctx, object_id)


class AOOKArgs(AOSRArgs):
    works: str = Field(description="Ответственные конструкции: «Фундаментная плита Пм1 в осях 1-5/А-Г»")
    aosr_refs: list[str] = Field(default_factory=list, description="АОСР на скрытые работы в составе конструкций")
    tests: list[str] = Field(default_factory=list, description="Испытания: протоколы прочности бетона и т.п.")
    permit: Literal["full", "partial", "conditional"] = "full"
    load_percent: int | None = None
    conditions: str | None = None


def build_aook(ctx: AgentContext, args: AOOKArgs) -> tuple[AOOKData, ConstructionObject]:
    obj = _object(ctx, args.object_id)
    p = docsvc.object_participants(ctx.db, ctx.org_id, obj.id, args.act_date)
    if p["developer"] is None:
        raise ValueError("У объекта не указан застройщик/технический заказчик — заполните карточку объекта")
    data = AOOKData(
        number=args.number, date=args.act_date, object_name=p["object_name"], developer=p["developer"],
        builder=p["builder"], designer=p["designer"], representatives=p["representatives"],
        other_representatives=p["other_representatives"], works=args.works, project_docs=args.project_docs,
        materials=args.materials, executive_docs=args.executive_docs, work_start=args.work_start,
        work_end=args.work_end, norms=args.norms, next_works=args.next_works, additional_info=args.additional_info,
        aosr_refs=args.aosr_refs, tests=args.tests, permit=args.permit, load_percent=args.load_percent,
        conditions=args.conditions,
    )
    return data, obj


@tool("create_aook_draft", "Создать черновик АООК (ответственные конструкции) с проверкой полноты.",
      scopes=("documents:write",))
def create_aook_draft(ctx: AgentContext, args: AOOKArgs) -> dict:
    data, obj = build_aook(ctx, args)
    doc = idsvc.create_aook(ctx.db, ctx.storage, org_id=ctx.org_id, data=data, created_by=f"agent:{ctx.agent_name}",
                            object_id=obj.id)
    ref = _doc_ref(doc)
    ctx.documents.append(ref)
    return ref | {"issues": doc.issues}


class ObjectArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)


@tool("id_registry", "Реестр исполнительной документации объекта и проверка: все ли скрытые работы закрыты АОСР.",
      scopes=("documents:read",))
def id_registry_tool(ctx: AgentContext, args: ObjectArgs) -> dict:
    obj = _object(ctx, args.object_id)
    return idsvc.id_registry(ctx.db, ctx.org_id, obj.id)


class JournalArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    period_start: dt.date
    period_end: dt.date
    create_document: bool = Field(False, description="Выгрузить журнал в XLSX в реестр документов")


@tool("work_journal", "Журнал выполнения работ за период (раздел 3 ОЖР) по учтённым объёмам.", scopes=("documents:read",))
def work_journal_tool(ctx: AgentContext, args: JournalArgs) -> dict:
    obj = _object(ctx, args.object_id)
    if args.create_document:
        doc = idsvc.export_work_journal(ctx.db, ctx.storage, ctx.org_id, obj.id, args.period_start, args.period_end,
                                        actor=f"agent:{ctx.agent_name}")
        ref = _doc_ref(doc)
        ctx.documents.append(ref)
        return {"document": ref}
    return {"entries": idsvc.work_journal(ctx.db, ctx.org_id, obj.id, args.period_start, args.period_end)[:200]}


class ArchiveCheckArgs(BaseModel):
    object_id: str | None = None


@tool("check_id_archive", "Проверка ИД по загруженному архиву: для каждого АОСР/АООК — есть ли в архиве документы о "
      "качестве, протоколы и схемы, на которые он ссылается; не указаны ли схема, шифр РД, даты.",
      scopes=("documents:read",))
def check_id_archive_tool(ctx: AgentContext, args: ArchiveCheckArgs) -> dict:
    from bis.services.id_check import check_archive, summary_text

    res = check_archive(ctx.db, ctx.org_id, _object(ctx, args.object_id).id if args.object_id else None)
    worst = [a for a in res["items"] if not a["complete"]][:20]
    return {k: res[k] for k in ("acts", "complete", "missing_refs", "with_issues")} | {
        "summary": summary_text(res),
        "problems": [{"act": a["title"], "missing": [f"{r['title']} № {r['number']}" for r in a["refs"] if not r["found"]],
                      "issues": a["issues"]} for a in worst]}


ID_TOOLS = [create_aook_draft, id_registry_tool, work_journal_tool, check_id_archive_tool]

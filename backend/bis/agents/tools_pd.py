"""Инструменты анализа проектной документации: комплектность, спецификации, сверка со сметой, замечания."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bis.agents.base import AgentContext, tool
from bis.agents.builtin_tools import _doc_ref, _uuid
from bis.agents.resolve import CONTRACT_HINT, OBJ_HINT, contract_row, obj_row
from bis.models import Document
from bis.services import project_docs as pd
from bis.services.documents import save_document


def _owned(ctx: AgentContext, model, value: str, what: str):
    row = ctx.db.get(model, _uuid(value, what))
    if row is None or row.organization_id != ctx.org_id:
        raise LookupError(f"Не найден(а): {what}")
    return row


class ObjectArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)


@tool("pd_completeness", "Комплектность проектной документации объекта по разделам (ПП РФ № 87) и комплектам РД.",
      scopes=("documents:read",))
def pd_completeness(ctx: AgentContext, args: ObjectArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    return pd.completeness(ctx.db, ctx.org_id, obj.id)


class DocArgs(BaseModel):
    document_id: str


def spec_for_document(ctx: AgentContext, doc: Document) -> tuple[pd.Specification, bool]:
    if doc.kind == 'project_doc' and doc.object_id:
        from bis.services.project_reading import read

        reading = read(ctx, doc.id, budget=8)
        rows = [item for page in reading.payload['pages'] for item in page['items'] if item['kind'] == 'specification' and item['quantity'] is not None]
        spec = pd.Specification(items=[pd.SpecItem(position=r['position'], name=r['name'], mark=r['mark'],
            unit=r['unit'], quantity=float(r['quantity'])) for r in rows])
        fallback = any(page['method'] == 'rules' for page in reading.payload['pages'])
        doc.data = {**doc.data, 'specification': spec.model_dump(), 'specification_fallback': fallback,
                    'specification_coverage': reading.payload['coverage'], 'specification_reading_id': str(reading.id)}
        return spec, fallback
    cached = (doc.data or {}).get("specification")
    if cached:
        return pd.Specification.model_validate(cached), bool((doc.data or {}).get("specification_fallback"))
    if not doc.file_key:
        raise ValueError("У документа нет файла")
    from bis.services.document_text import document_text

    text = document_text(ctx.db, doc, ctx.storage)
    spec, fallback = pd.extract_specification(ctx.gateway, text)
    doc.data = {**(doc.data or {}), "specification": spec.model_dump(), "specification_fallback": fallback}
    return spec, fallback


@tool("extract_specification", "Извлечь из проектного документа спецификацию/ведомость: наименование, ед., количество.",
      scopes=("documents:read",))
def extract_specification(ctx: AgentContext, args: DocArgs) -> dict:
    doc = _owned(ctx, Document, args.document_id, "документ")
    spec, fallback = spec_for_document(ctx, doc)
    return {"items": [i.model_dump() for i in spec.items[:300]], "count": len(spec.items),
            "method": "правила" if fallback else "модель", 'coverage': (doc.data or {}).get('specification_coverage'),
            'reading_id': (doc.data or {}).get('specification_reading_id'), 'truncated': len(spec.items) > 300}


class CompareArgs(BaseModel):
    document_id: str
    contract_id: str | None = Field(None, description=CONTRACT_HINT)
    tolerance_pct: float = 5.0


@tool("compare_spec_with_estimate", "Сверить спецификацию проекта со сметой договора: расхождения объёмов и позиции, "
      "отсутствующие в смете.", scopes=("documents:read",))
def compare_spec_with_estimate(ctx: AgentContext, args: CompareArgs) -> dict:
    doc = _owned(ctx, Document, args.document_id, "документ")
    contract = contract_row(ctx, args.contract_id)
    spec, _ = spec_for_document(ctx, doc)
    return pd.compare_with_estimate(ctx.db, ctx.org_id, contract.id, spec, args.tolerance_pct)


class RemarksArgs(BaseModel):
    object_id: str | None = None
    project: str = Field(description="Шифр и наименование документации")
    remarks: list[pd.Remark]


@tool("create_remarks_sheet", "Сформировать ведомость замечаний к проектной документации (DOCX).",
      scopes=("documents:write",))
def create_remarks_sheet(ctx: AgentContext, args: RemarksArgs) -> dict:
    obj = obj_row(ctx, args.object_id) if args.object_id else None
    content = pd.render_remarks(object_name=obj.name if obj else "", project=args.project, remarks=args.remarks,
                                author=ctx.user.full_name)
    doc = save_document(ctx.db, ctx.storage, org_id=ctx.org_id, kind="remarks",
                        title=f"Ведомость замечаний: {args.project[:200]}",
                        data={"remarks": [r.model_dump() for r in args.remarks]}, file_bytes=content,
                        file_name="Ведомость_замечаний.docx", created_by=f"agent:{ctx.agent_name}",
                        object_id=obj.id if obj else None)
    ref = _doc_ref(doc)
    ctx.documents.append(ref)
    return ref | {"remarks": len(args.remarks)}


PD_TOOLS = [pd_completeness, extract_specification, compare_spec_with_estimate, create_remarks_sheet]

"""Встроенные инструменты агентов: поиск знаний, реестры, создание документов."""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select

from bis.agents.base import AgentContext, tool
from bis.agents.resolve import OBJ_HINT, AmbiguousReference, contract_ref, object_ref
from bis.documents import AOSRData, LetterData
from bis.documents.aosr import REPRESENTATIVE_ROLES
from bis.models import ConstructionObject, Contract, Counterparty, Document, KnowledgeSource, Organization
from bis.rag.search import hybrid_search
from bis.services import documents as docsvc

SCOPE_KINDS = {"norms": ["norm"], "organization": ["org_document", "letter", "contract", "project_doc"], "project": ["org_document", "letter", "contract", "project_doc"], "all": None}


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise ValueError(f"Некорректный идентификатор {what}: {value}") from e


def _doc_ref(doc: Document) -> dict:
    return {"id": str(doc.id), "kind": doc.kind, "title": doc.title, "status": doc.status,
            "number": doc.number, "download_url": f"/api/documents/{doc.id}/file"}


# --- база знаний --------------------------------------------------------------------------------


class SearchArgs(BaseModel):
    query: str = Field(description="Поисковый запрос своими словами или ссылка «СП 70.13330.2012 п. 5.16»")
    scope: Literal["norms", "organization", "project", "all"] = Field("all", description="norms — нормативы; organization — документы компании")
    k: int = Field(6, ge=1, le=15)


@tool("search_knowledge", "Поиск по базе знаний: нормативы (СП, ГОСТ, СНиП) и документы организации. "
      "Возвращает фрагменты с номером [n] для ссылок.", scopes=("knowledge:read",))
def search_knowledge(ctx: AgentContext, args: SearchArgs) -> dict:
    policy = ctx.db.info.get("source_context")
    level = {"norms": "knowledge", "organization": "organization", "project": "project"}.get(args.scope) if policy and policy.strict else None
    hits = hybrid_search(ctx.db, ctx.embedder, args.query, org_id=ctx.org_id, kinds=SCOPE_KINDS[args.scope], library_level=level,
                         include_global=args.scope != "organization", k=args.k)
    results = []
    for h in hits:
        d = h.to_dict()
        n = ctx.add_citation(d)
        results.append({"n": n, "citation": h.citation, "status": h.status, "section": h.section_path,
                        "document_id": h.document_id, "page": h.page, "content": h.content[:3000]})
    return {"results": results}


# --- документы организации (в т.ч. загруженный архив) ---------------------------------------------

ORG_KINDS = SCOPE_KINDS["organization"]
_WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.IGNORECASE)
_STOP = {"найди", "покажи", "открой", "какие", "какой", "какая", "есть", "все", "всё", "документ", "документы", "про",
         "для", "что", "где", "нам", "наш", "наши", "по", "на", "мне", "пожалуйста"}


def share_document(ctx: AgentContext, doc: Document) -> dict:
    """Документ показывается пользователю карточкой под ответом (открыть/скачать)."""
    from bis.services.library_access import assert_document

    assert_document(ctx.db, doc)
    ref = _doc_ref(doc)
    if all(d.get("id") != ref["id"] for d in ctx.documents):
        ctx.documents.append(ref)
    return ref


def _first_chunk(ctx: AgentContext, doc: Document) -> dict | None:
    from bis.models import KnowledgeChunk
    from bis.rag.search import _hit

    row = ctx.db.execute(select(KnowledgeChunk, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                         .where(KnowledgeSource.document_id == doc.id).order_by(KnowledgeChunk.ord).limit(1)).first()
    return _hit(*row).to_dict() if row else None


class FindDocumentsArgs(BaseModel):
    workspace: Literal["platform", "executive"] = "platform"
    query: str | None = Field(None, description="Что ищем своими словами: «АОСР на армирование плиты», "
                              "«письмо заказчика про исполнительную документацию», «сертификат на арматуру», номер")
    kind: str | None = Field(None, description="aosr, aook, scheme, contract, incoming_letter, letter, ks2, ks3, order, "
                             "project_doc, certificate, test_protocol, supply_doc, work_journal, photo, other")
    object_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = Field(8, ge=1, le=30)

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def _ru_date(cls, v):
        """Модель часто пишет дату по-русски: 18.03.2026."""
        if isinstance(v, str) and (m := re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", v)):
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return v


@tool("find_documents", "Найти документы организации — и созданные в BIS, и загруженный архив (АОСР, схемы, договоры, "
      "письма, сертификаты, протоколы, приказы) — по смыслу текста и по названию. Возвращает дату, объект, выдержку "
      "из текста; найденные документы пользователь видит карточками под ответом.", scopes=("documents:read",))
def find_documents(ctx: AgentContext, args: FindDocumentsArgs) -> dict:
    def allowed(stmt):
        stmt = stmt.where(Document.organization_id == ctx.org_id)
        if ctx.workspace == "executive" or args.workspace == "executive":
            from bis.modules.executive import DOCUMENT_KINDS, SOURCE_KINDS

            stmt = stmt.where(Document.kind.in_((*DOCUMENT_KINDS, *SOURCE_KINDS)))
        if args.kind:
            # при поиске конкретного письма модель путает входящее и исходящее; список «входящих» — строго
            letters = args.query and args.kind in ("letter", "incoming_letter")
            kinds = ["letter", "incoming_letter"] if letters else [args.kind]
            stmt = stmt.where(Document.kind.in_(kinds))
        if args.object_id:
            stmt = stmt.where(or_(Document.object_id == object_ref(ctx, args.object_id), Document.object_id.is_(None)))
        if args.date_from:
            stmt = stmt.where(Document.date >= args.date_from)
        if args.date_to:
            stmt = stmt.where(Document.date <= args.date_to)
        return stmt

    ranked: dict[uuid.UUID, dict] = {}
    if args.query and args.query.strip():
        number = re.search(r"№\s*([\w./-]+)", args.query)
        exact = number[1] if number else args.query.strip() if re.fullmatch(r"[\d./-]+", args.query.strip()) else None
        if exact:
            for d in ctx.db.scalars(allowed(select(Document)).where(or_(
                    Document.number == exact, Document.data["card"]["number"].astext == exact))).all():
                ranked[d.id] = {"doc": d, "score": 5.0, "excerpt": None, "n": None}
        words = [w for w in _WORD.findall(args.query.lower()) if w not in _STOP]
        # 0. по карточке «Синапса»: марки (B25, А500С), оси, конструкции (Фм1), номера ссылок, шифр РД
        tokens = [t for t in re.findall(r"[A-Za-zА-Яа-яЁё]*\d[\w.\-/]*|[А-ЯA-Z]{2,4}\d?", args.query) if len(t) >= 2]
        if tokens:
            for d in ctx.db.scalars(allowed(select(Document)).where(
                    *(Document.data["card_text"].astext.ilike(f"%{t}%") for t in tokens[:4])).limit(50)).all():
                ranked.setdefault(d.id, {"doc": d, "score": 1.5, "excerpt": None, "n": None})
        # 1. по названию и номеру: чем больше слов запроса в названии, тем выше
        if words:
            stems = [w[:max(4, len(w) - 2)] for w in words]
            rows = ctx.db.scalars(allowed(select(Document)).where(or_(*(Document.title.ilike(f"%{t}%") for t in stems),
                                                                       *(Document.number.ilike(f"%{t}%") for t in stems)))
                                  .limit(300)).all()
            for d in rows:
                low = f"{d.title} {d.number or ''}".lower()
                share = sum(1 for t in stems if t in low) / len(stems)
                if share >= 0.5 or len(stems) == 1:
                    ranked.setdefault(d.id, {"doc": d, "score": 1.0 + share, "excerpt": None, "n": None})
        # 2. по смыслу текста (в т.ч. распознанных сканов)
        hits = hybrid_search(ctx.db, ctx.embedder, args.query, org_id=ctx.org_id, kinds=ORG_KINDS, include_global=False,
                             k=min(args.limit * 3, 30))
        src_doc = dict(ctx.db.execute(select(KnowledgeSource.id, KnowledgeSource.document_id).where(
            KnowledgeSource.id.in_([uuid.UUID(h.source_id) for h in hits]))).all()) if hits else {}
        ids = {src_doc.get(uuid.UUID(h.source_id)) for h in hits} - {None}
        docs = {d.id: d for d in ctx.db.scalars(allowed(select(Document)).where(Document.id.in_(ids))).all()} if ids else {}
        for rank, h in enumerate(hits):
            d = docs.get(src_doc.get(uuid.UUID(h.source_id)))
            if d is None:
                continue
            item = ranked.setdefault(d.id, {"doc": d, "score": 0.0, "excerpt": None, "n": None})
            item["score"] += 1.0 / (rank + 2)
            if item["excerpt"] is None:
                item["excerpt"] = h.content[:700]
                item["n"] = ctx.add_citation(h.to_dict())
    else:
        for d in ctx.db.scalars(allowed(select(Document)).order_by(Document.date.desc().nulls_last(),
                                                                    Document.created_at.desc()).limit(args.limit)).all():
            ranked[d.id] = {"doc": d, "score": 0.0, "excerpt": None, "n": None}

    best = sorted(ranked.values(), key=lambda x: -x["score"])[: args.limit]
    for x in best:  # найден по названию — выдержка из начала текста, чтобы на документ можно было сослаться [n]
        if x["excerpt"] is None:
            hit = _first_chunk(ctx, x["doc"])
            if hit:
                x["excerpt"], x["n"] = hit["content"][:700], ctx.add_citation(hit)
    objects = {o.id: o.name for o in ctx.db.scalars(select(ConstructionObject).where(
        ConstructionObject.id.in_([x["doc"].object_id for x in best if x["doc"].object_id]))).all()} if best else {}
    out = []
    cards = len(best) if (args.kind or not args.query) else 3  # список по виду — все; поиск по смыслу — самые близкие
    for i, x in enumerate(best):
        d = x["doc"]
        if i < cards:
            share_document(ctx, d)
        card = (d.data or {}).get("card") or {}
        out.append({"id": str(d.id), "title": d.title, "kind": d.kind, "number": d.number, "date": d.date,
                    "status": d.status, "object": objects.get(d.object_id), "card": card.get("title"),
                    "n": x["n"], "excerpt": x["excerpt"]})
    if not out and args.query and (args.kind or args.date_from or args.date_to):
        # модель могла ошибиться видом документа или датой — ищем ещё раз без этих фильтров
        res = find_documents.handler(ctx, args.model_copy(update={"kind": None, "date_from": None, "date_to": None}))
        return res | {"note": "С указанными видом и датой ничего не нашлось — показан поиск без этих фильтров"}
    return {"documents": out, "found": len(out),
            "hint": None if out else "Ничего не нашлось — попробуйте другие слова или поиск по базе знаний"}


def resolve_document(ctx: AgentContext, ref: str) -> Document:
    """Документ по id, а если модель передала номер («45», «№ 4512») — по номеру документа."""
    try:
        doc = ctx.db.get(Document, uuid.UUID(str(ref)))
    except ValueError:
        num = re.sub(r"^\s*(?:(?:договор|письмо|акт|АОСР|АООК|сертификат|протокол)\s*)?№?\s*", "", str(ref), flags=re.I).strip()
        docs = ctx.db.scalars(select(Document).where(Document.organization_id == ctx.org_id, or_(
            Document.number == num, Document.data["card"]["number"].astext == num,
            Document.title.ilike(str(ref).strip()))).limit(6)).all()
        if len(docs) > 1:
            raise AmbiguousReference("С этим номером несколько документов — выберите: " + "; ".join(
                d.title for d in docs), [_doc_ref(d) for d in docs]) from None
        doc = docs[0] if docs else None
    if doc is None or doc.organization_id != ctx.org_id:
        raise LookupError("Документ не найден — найдите его find_documents")
    from bis.services.library_access import assert_document

    assert_document(ctx.db, doc)
    return doc


class CardArgs(BaseModel):
    document_id: str


@tool("document_card", "Карточка документа из «Синапса»: вид, №, дата, стороны, РД (шифр, листы, изм.), конструкции, "
      "материалы и марки, оси, отметки, нормы, суть — и связи: на какие документы ссылается (найдены ли в архиве) "
      "и какие документы ссылаются на него.", scopes=("documents:read",))
def document_card(ctx: AgentContext, args: CardArgs) -> dict:
    from bis.brain.links import neighbours

    doc = resolve_document(ctx, args.document_id)
    share_document(ctx, doc)
    card = (doc.data or {}).get("card")
    hit = _first_chunk(ctx, doc)
    n = ctx.add_citation(hit) if hit else None
    return {"id": str(doc.id), "title": doc.title, "kind": doc.kind, "card": card or "карточка ещё строится", "n": n,
            **neighbours(ctx.db, doc)}


class CorrespondenceArgs(BaseModel):
    object_id: str | None = None


@tool("correspondence_status", "Переписка по объекту: запросы заказчика и ведомств — ответили ли и не прошёл ли срок; "
      "наши письма — какие документы в них названы и каких нет в архиве, к какому сроку. Для вопросов «что не "
      "отправили», «ответили ли», «что обещали», «что ждёт ответа».", scopes=("documents:read",))
def correspondence_status_tool(ctx: AgentContext, args: CorrespondenceArgs) -> dict:
    from bis.brain.chains import correspondence

    res = correspondence(ctx.db, ctx.org_id, object_ref(ctx, args.object_id) if args.object_id else None)
    return {"today": res["today"], "open": res["open"][:25], "requests": res["requests"][:20],
            "promises": res["promises"][:20]}


# --- реестры ------------------------------------------------------------------------------------


class FindArgs(BaseModel):
    query: str | None = Field(None, description="Часть названия, шифра, ИНН или номера")
    limit: int = Field(10, ge=1, le=50)


@tool("find_objects", "Поиск объектов строительства организации.", scopes=("registry:read",))
def find_objects(ctx: AgentContext, args: FindArgs) -> dict:
    stmt = select(ConstructionObject).where(ConstructionObject.organization_id == ctx.org_id)
    if args.query:
        q = f"%{args.query}%"
        stmt = stmt.where(or_(ConstructionObject.name.ilike(q), ConstructionObject.code.ilike(q),
                              ConstructionObject.address.ilike(q)))
    rows = ctx.db.scalars(stmt.limit(args.limit)).all()
    return {"objects": [{"id": str(o.id), "name": o.name, "code": o.code, "address": o.address, "status": o.status}
                        for o in rows]}


class FindCounterpartyArgs(FindArgs):
    kind: str | None = Field(None, description="customer, developer, designer, subcontractor, supplier")


@tool("find_counterparties", "Поиск контрагентов (заказчики, проектировщики, субподрядчики, поставщики).",
      scopes=("registry:read",))
def find_counterparties(ctx: AgentContext, args: FindCounterpartyArgs) -> dict:
    stmt = select(Counterparty).where(Counterparty.organization_id == ctx.org_id)
    if args.query:
        q = f"%{args.query}%"
        stmt = stmt.where(or_(Counterparty.name.ilike(q), Counterparty.inn.ilike(q)))
    if args.kind:
        stmt = stmt.where(Counterparty.kind == args.kind)
    rows = ctx.db.scalars(stmt.limit(args.limit)).all()
    return {"counterparties": [{"id": str(c.id), "name": c.name, "kind": c.kind, "inn": c.inn, "address": c.address,
                                "head_position": c.head_position, "head_name": c.head_name, "email": c.email}
                               for c in rows]}


class FindContractArgs(FindArgs):
    object_id: str | None = None


@tool("find_contracts", "Поиск договоров (подряд, субподряд, поставка) с контрагентами и сроками.",
      scopes=("registry:read",))
def find_contracts(ctx: AgentContext, args: FindContractArgs) -> dict:
    stmt = select(Contract).where(Contract.organization_id == ctx.org_id)
    if args.query:
        q = f"%{args.query}%"
        stmt = stmt.where(or_(Contract.number.ilike(q), Contract.subject.ilike(q)))
    if args.object_id:
        stmt = stmt.where(Contract.object_id == object_ref(ctx, args.object_id))
    out = []
    for c in ctx.db.scalars(stmt.limit(args.limit)).all():
        cp = ctx.db.get(Counterparty, c.counterparty_id) if c.counterparty_id else None
        out.append({"id": str(c.id), "kind": c.kind, "number": c.number, "date": c.date, "subject": c.subject,
                    "counterparty": cp.name if cp else None, "amount": c.amount, "start_date": c.start_date,
                    "end_date": c.end_date, "status": c.status,
                    "document_id": str(c.document_id) if c.document_id else None})
    docs = select(Document).where(Document.organization_id == ctx.org_id, Document.kind == "contract")
    if args.object_id:
        docs = docs.where(Document.object_id == object_ref(ctx, args.object_id))
    if args.query:
        docs = docs.where(or_(Document.title.ilike(f"%{args.query}%"), Document.number.ilike(f"%{args.query}%")))
    archive = [{"document_id": str(d.id), "title": d.title, "number": d.number, "date": d.date}
               for d in ctx.db.scalars(docs.order_by(Document.created_at.desc()).limit(args.limit))]
    return {"contracts": out, "archive_contracts": archive,
            "hint": "Файлы договоров доступны через get_document(document_id), даже если реестр договоров пуст."}


class ObjectArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)


@tool("get_object_participants", "Участники объекта для исполнительной документации: застройщик, проектировщик, "
      "генподрядчик и представители по приказам (с номерами НРС).", scopes=("registry:read",))
def get_object_participants(ctx: AgentContext, args: ObjectArgs) -> dict:
    p = docsvc.object_participants(ctx.db, ctx.org_id, object_ref(ctx, args.object_id))
    reps = {role: rep.format(REPRESENTATIVE_ROLES[role]["nrs"]) for role, rep in p["representatives"].items()}
    missing = [m["title"] for r, m in REPRESENTATIVE_ROLES.items() if m["required"] and r not in p["representatives"]]
    return {
        "object": p["object_name"],
        "developer": p["developer"].format() if p["developer"] else None,
        "designer": p["designer"].format() if p["designer"] else None,
        "builder": p["builder"].format(),
        "representatives": reps,
        "other_representatives": [r.format() for r in p["other_representatives"]],
        "missing_required_representatives": missing,
    }


class ListDocumentsArgs(BaseModel):
    kind: str | None = Field(None, description="aosr, aook, ks2, ks3, scheme, m29, writeoff, letter, incoming_letter, contract, order, project_doc …")
    object_id: str | None = None
    status: str | None = Field(None, description="draft, approved, rejected, signed")
    limit: int = Field(20, ge=1, le=100)


@tool("list_documents", "Реестр документов организации с фильтрами.", scopes=("documents:read",))
def list_documents(ctx: AgentContext, args: ListDocumentsArgs) -> dict:
    stmt = select(Document).where(Document.organization_id == ctx.org_id)
    if args.kind:
        stmt = stmt.where(Document.kind == args.kind)
    if args.object_id:
        stmt = stmt.where(Document.object_id == object_ref(ctx, args.object_id))
    if args.status:
        stmt = stmt.where(Document.status == args.status)
    rows = ctx.db.scalars(stmt.order_by(Document.date.desc().nulls_last(), Document.created_at.desc()).limit(args.limit)).all()
    return {"documents": [share_document(ctx, d) | {"date": d.date} for d in rows]}


class EmptyArgs(BaseModel):
    pass


@tool("data_coverage", "Проверить охват архива: сколько документов проиндексировано, сколько ждут чтения, OCR, "
      "карточек или зрения. Отличает отсутствие данных от сбоя обработки.", scopes=("documents:read",))
def data_coverage(ctx: AgentContext, args: EmptyArgs) -> dict:
    from bis.brain.build import coverage

    return coverage(ctx.db, ctx.org_id)


@tool("get_organization_info", "Реквизиты своей организации и подписант по умолчанию.", scopes=("registry:read",))
def get_organization_info(ctx: AgentContext, args: EmptyArgs) -> dict:
    org = ctx.db.get(Organization, ctx.org_id)
    return {"name": org.name, "inn": org.inn, "kpp": org.kpp, "ogrn": org.ogrn, "address": org.address,
            "phone": org.phone, "email": org.email, "default_signer": (org.settings or {}).get("default_signer")}


# --- документы ----------------------------------------------------------------------------------


class AOSRArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    works: str = Field(description="Наименование скрытых работ с указанием осей/отметок/захватки")
    project_docs: str = Field(description="Шифр и листы рабочей документации, наименование, разработчик")
    materials: list[str] = Field(default_factory=list, description="Материалы со ссылками на сертификаты/паспорта")
    executive_docs: list[str] = Field(default_factory=list, description="Исполнительные схемы, протоколы испытаний")
    work_start: date
    work_end: date
    norms: list[str] = Field(default_factory=list, description="Нормативы с пунктами и разделы ПД")
    next_works: str | None = None
    additional_info: str | None = None
    number: str | None = None
    act_date: date | None = None


@tool("create_aosr_draft", "Создать черновик АОСР по объекту. Участники и представители подставляются из реестра "
      "по приказам. Возвращает ссылку на документ и замечания проверки.", scopes=("documents:write",))
def create_aosr_draft(ctx: AgentContext, args: AOSRArgs) -> dict:
    object_id = object_ref(ctx, args.object_id)
    p = docsvc.object_participants(ctx.db, ctx.org_id, object_id, args.act_date)
    if p["developer"] is None:
        raise ValueError("У объекта не указан застройщик/технический заказчик — заполните карточку объекта")
    data = AOSRData(
        number=args.number, date=args.act_date, object_name=p["object_name"], developer=p["developer"],
        builder=p["builder"], designer=p["designer"], representatives=p["representatives"],
        other_representatives=p["other_representatives"], works=args.works, project_docs=args.project_docs,
        materials=args.materials, executive_docs=args.executive_docs, work_start=args.work_start,
        work_end=args.work_end, norms=args.norms, next_works=args.next_works, additional_info=args.additional_info,
    )
    doc = docsvc.create_aosr(ctx.db, ctx.storage, org_id=ctx.org_id, data=data, created_by=f"agent:{ctx.agent_name}",
                             object_id=object_id)
    ref = _doc_ref(doc)
    ctx.documents.append(ref)
    return ref | {"issues": doc.issues}


class LetterArgs(BaseModel):
    addressee: list[str] = Field(description="Строки адресата в дательном падеже: должность, организация, ФИО")
    subject: str = Field(description="Заголовок к тексту, начинается с «О/Об …»")
    greeting: str | None = Field(None, description="«Уважаемый Иван Иванович!»")
    body: list[str] = Field(description="Абзацы текста")
    attachments: list[str] = Field(default_factory=list)
    in_reply_to: str | None = None
    signer_position: str | None = None
    signer_name: str | None = None
    object_id: str | None = None
    contract_id: str | None = None


@tool("create_letter_draft", "Создать черновик исходящего письма на бланке организации.", scopes=("documents:write",))
def create_letter_draft(ctx: AgentContext, args: LetterArgs) -> dict:
    org = ctx.db.get(Organization, ctx.org_id)
    signer = (org.settings or {}).get("default_signer") or {}
    data = LetterData(
        sender=docsvc.party_from_org(org), in_reply_to=args.in_reply_to, addressee=args.addressee,
        subject=args.subject, greeting=args.greeting, body=args.body, attachments=args.attachments,
        signer_position=args.signer_position or signer.get("position") or (ctx.user.position or ""),
        signer_name=args.signer_name or signer.get("name") or ctx.user.full_name,
        executor=ctx.user.full_name,
    )
    doc = docsvc.create_letter(
        ctx.db, ctx.storage, org_id=ctx.org_id, data=data, created_by=f"agent:{ctx.agent_name}",
        object_id=object_ref(ctx, args.object_id) if args.object_id else None,
        contract_id=contract_ref(ctx, args.contract_id) if args.contract_id else None,
    )
    ref = _doc_ref(doc)
    ctx.documents.append(ref)
    return ref


BUILTIN_TOOLS = [
    search_knowledge,
    find_documents,
    document_card,
    correspondence_status_tool,
    find_objects,
    find_counterparties,
    find_contracts,
    get_object_participants,
    list_documents,
    get_organization_info,
    data_coverage,
    create_aosr_draft,
    create_letter_draft,
]

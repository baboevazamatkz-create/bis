"""One read policy shared by API, RAG, agents and background executive jobs."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, event, false, func, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from bis.models import ConstructionObject, Document, KnowledgeChunk, KnowledgeSource, Organization
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion


@dataclass(frozen=True)
class SourceContext:
    organization_id: uuid.UUID
    object_id: uuid.UUID | None
    strict: bool


def strict_enabled(db, org_id):
    org = db.get(Organization, org_id)
    return bool(org and (org.settings or {}).get("libraries_strict"))


def bind_context(db, org_id, object_id=None):
    project = uuid.UUID(str(object_id)) if object_id else None
    if project:
        obj = db.get(ConstructionObject, project)
        if obj is None or obj.organization_id != org_id:
            raise LookupError("Объект не найден")
    scope = SourceContext(org_id, project, strict_enabled(db, org_id))
    db.info["source_context"] = scope
    return scope


def document_predicate(scope):
    return and_(Document.organization_id == scope.organization_id, or_(
        Document.object_id == scope.object_id if scope.object_id else false(),
        and_(Document.object_id.is_(None), func.jsonb_extract_path_text(Document.data, "library_scope") == "organization")),
        or_(func.jsonb_extract_path_text(Document.data, "library_version_id").is_(None),
            Document.id.in_(select(LibraryVersion.document_id).where(LibraryVersion.id.in_(active_versions(scope))))))


def connection_predicate(scope):
    return or_(LibraryConnection.level == "knowledge", and_(
        LibraryConnection.organization_id == scope.organization_id, or_(
            LibraryConnection.level == "organization",
            and_(LibraryConnection.level == "project", LibraryConnection.object_id == scope.object_id)
            if scope.object_id else false())))


def active_versions(scope):
    return select(LibraryVersion.id).join(LibraryFile, LibraryFile.active_version_id == LibraryVersion.id).join(
        LibraryConnection, LibraryConnection.id == LibraryFile.connection_id).where(
            connection_predicate(scope), LibraryConnection.enabled.is_(True), LibraryFile.state == "ready",
            LibraryVersion.state == "ready")


def source_predicate(scope):
    version_sources = select(LibraryVersion.source_id).where(LibraryVersion.id.in_(active_versions(scope)))
    # Legacy/project uploads remain available only in their explicit project; global demo norms never enter strict RAG.
    project_docs = select(Document.id).where(document_predicate(scope),
        Document.status.notin_(["cancelled", "replaced", "rejected"]),
        func.jsonb_extract_path_text(Document.data, "library_version_id").is_(None))
    return or_(KnowledgeSource.id.in_(version_sources), and_(
        KnowledgeSource.organization_id == scope.organization_id, KnowledgeSource.document_id.in_(project_docs)))


@event.listens_for(Session, "do_orm_execute")
def enforce_reads(state):
    scope = state.session.info.get("source_context")
    if not state.is_select or not scope or state.execution_options.get("library_management"):
        return
    from bis.models.facts import BrainInvestigation, FactConflict, FactDecision, FactSnapshot, ProjectFact
    from bis.models.project_reading import ProjectReading

    for model in (FactSnapshot, ProjectFact, FactConflict, FactDecision, BrainInvestigation, ProjectReading):
        predicate = and_(model.organization_id == scope.organization_id,
            model.object_id == scope.object_id if scope.object_id else false())
        state.statement = state.statement.options(with_loader_criteria(model, predicate, include_aliases=True))
    if not scope.strict:
        return
    from bis.models import (
        Appointment,
        ChatSession,
        Contract,
        ContractObligation,
        DocumentLink,
        IntakeEvent,
        OrgMemory,
        Task,
    )
    from bis.models.works import EstimateItem, MaterialReceipt, MaterialUsage, ScheduleTask, WorkVolume

    allowed_docs = select(Document.id).where(document_predicate(scope))
    allowed_sources = select(KnowledgeSource.id).where(source_predicate(scope),
                                                     KnowledgeSource.status.notin_(["cancelled", "replaced"]))
    stmt = state.statement.options(
        with_loader_criteria(Document, document_predicate(scope), include_aliases=True),
        with_loader_criteria(KnowledgeSource, source_predicate(scope), include_aliases=True),
        with_loader_criteria(KnowledgeChunk, KnowledgeChunk.source_id.in_(allowed_sources), include_aliases=True),
        with_loader_criteria(DocumentLink, and_(DocumentLink.from_document_id.in_(allowed_docs),
            or_(DocumentLink.to_document_id.is_(None), DocumentLink.to_document_id.in_(allowed_docs))), include_aliases=True),
        with_loader_criteria(ChatSession, and_(ChatSession.organization_id == scope.organization_id,
            ChatSession.object_id == scope.object_id if scope.object_id else ChatSession.object_id.is_(None)), include_aliases=True),
        with_loader_criteria(OrgMemory, and_(OrgMemory.organization_id == scope.organization_id,
            or_(OrgMemory.object_id.is_(None), OrgMemory.object_id == scope.object_id) if scope.object_id else OrgMemory.object_id.is_(None)), include_aliases=True))
    for model in (Contract, Appointment, Task, EstimateItem, ScheduleTask, WorkVolume, MaterialReceipt, MaterialUsage):
        if hasattr(model, "object_id"):
            stmt = stmt.options(with_loader_criteria(model, and_(model.organization_id == scope.organization_id,
                model.object_id == scope.object_id if scope.object_id else false()), include_aliases=True))
    stmt = stmt.options(with_loader_criteria(IntakeEvent, and_(IntakeEvent.organization_id == scope.organization_id,
        or_(IntakeEvent.object_id.is_(None), IntakeEvent.object_id == scope.object_id)
        if scope.object_id else IntakeEvent.object_id.is_(None)), include_aliases=True))
    stmt = stmt.options(with_loader_criteria(ContractObligation,
        ContractObligation.contract_id.in_(select(Contract.id).where(Contract.organization_id == scope.organization_id,
            Contract.object_id == scope.object_id if scope.object_id else false())), include_aliases=True))
    state.statement = stmt


def assert_document(db, doc):
    scope = db.info.get("source_context")
    if scope and scope.strict and doc:
        allowed = db.scalar(select(Document.id).where(Document.id == doc.id, document_predicate(scope)))
        if allowed is None:
            raise LookupError("Документ недоступен в выбранном проекте")

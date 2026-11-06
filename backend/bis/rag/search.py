"""Гибридный поиск: векторный (pgvector) + полнотекстовый (PostgreSQL, русская морфология)
+ точный поиск по ссылке «СП … п. …», объединение Reciprocal Rank Fusion, опционально реранкер."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.orm import Session

from bis.config import get_settings
from bis.models import KnowledgeChunk, KnowledgeSource
from bis.rag.chunking import parse_reference
from bis.rag.embeddings import Embedder

_TOKEN_RE = re.compile(r"[а-яёa-z]{3,}|\d+", re.IGNORECASE)
RRF_K = 60

STATUS_LABELS = {"cancelled": "отменён", "replaced": "заменён", "unknown": "статус не проверен"}


@dataclass
class SearchHit:
    chunk_id: str
    source_id: str
    title: str
    kind: str
    designation: str | None
    clause: str | None
    section_path: str | None
    heading: str | None
    content: str
    page: int | None
    status: str
    score: float = 0.0
    n: int = 0

    @property
    def citation(self) -> str:
        if self.designation:
            base = self.designation
            if self.clause:
                base += f", п. {self.clause}"
            elif self.heading:
                base += f", {self.heading}"
        else:
            base = self.title
            if self.page:
                base += f", стр. {self.page}"
        if self.status in STATUS_LABELS:
            base += f" (документ {STATUS_LABELS[self.status]}!)"
        return base

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["citation"] = self.citation
        return d


def build_tsquery(query: str) -> str | None:
    tokens = []
    for t in _TOKEN_RE.findall(query.lower()):
        if t not in tokens:
            tokens.append(t)
    return " | ".join(tokens[:30]) or None


def _base_filter(stmt: Select, org_id: uuid.UUID | None, kinds: list[str] | None, include_global: bool,
                 only_active: bool, source_ids: list[uuid.UUID] | None) -> Select:
    stmt = stmt.join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
    scope = []
    if org_id is not None:
        scope.append(KnowledgeChunk.organization_id == org_id)
    if include_global:
        scope.append(KnowledgeChunk.organization_id.is_(None))
    stmt = stmt.where(or_(*scope)) if scope else stmt.where(false())
    if kinds:
        stmt = stmt.where(KnowledgeSource.kind.in_(kinds))
    if only_active:
        stmt = stmt.where(KnowledgeSource.status.notin_(["cancelled", "replaced"]))
    if source_ids:
        stmt = stmt.where(KnowledgeSource.id.in_(source_ids))
    return stmt


def _hit(chunk: KnowledgeChunk, src: KnowledgeSource) -> SearchHit:
    return SearchHit(
        chunk_id=str(chunk.id),
        source_id=str(src.id),
        title=src.title,
        kind=src.kind,
        designation=src.designation,
        clause=chunk.clause,
        section_path=chunk.section_path,
        heading=chunk.heading,
        content=chunk.content,
        page=chunk.page,
        status=src.status,
    )


def hybrid_search(
    db: Session,
    embedder: Embedder,
    query: str,
    *,
    org_id: uuid.UUID | None,
    kinds: list[str] | None = None,
    include_global: bool = True,
    only_active: bool = False,
    source_ids: list[uuid.UUID] | None = None,
    k: int = 8,
) -> list[SearchHit]:
    query = query.strip()
    if not query:
        return []
    pool = max(k * 4, 20)
    flt = dict(org_id=org_id, kinds=kinds, include_global=include_global, only_active=only_active, source_ids=source_ids)
    ranked: list[list[tuple[KnowledgeChunk, KnowledgeSource]]] = []

    qvec = embedder.embed_query(query)

    # 1. Точная ссылка на пункт
    designation, clause = parse_reference(query)
    if designation:
        stmt = _base_filter(select(KnowledgeChunk, KnowledgeSource), **flt).where(
            func.replace(KnowledgeSource.designation, " ", "").ilike("%" + designation.replace(" ", "") + "%")
        )
        if clause:
            stmt = stmt.where(or_(KnowledgeChunk.clause == clause, KnowledgeChunk.clause.like(clause + ".%")))
            ranked.append(list(db.execute(stmt.order_by(KnowledgeChunk.ord).limit(5)).all()))
        else:
            stmt = stmt.order_by(KnowledgeChunk.embedding.cosine_distance(qvec)).limit(pool)
            ranked.append(list(db.execute(stmt).all()))

    # 2. Векторный поиск
    stmt = _base_filter(select(KnowledgeChunk, KnowledgeSource), **flt).where(KnowledgeChunk.embedding.is_not(None))
    stmt = stmt.order_by(KnowledgeChunk.embedding.cosine_distance(qvec)).limit(pool)
    ranked.append(list(db.execute(stmt).all()))

    # 3. Полнотекстовый поиск
    tsq_text = build_tsquery(query)
    if tsq_text:
        tsq = func.to_tsquery("russian", tsq_text)
        stmt = _base_filter(select(KnowledgeChunk, KnowledgeSource), **flt).where(KnowledgeChunk.tsv.op("@@")(tsq))
        stmt = stmt.order_by(func.ts_rank_cd(KnowledgeChunk.tsv, tsq).desc()).limit(pool)
        ranked.append(list(db.execute(stmt).all()))

    scores: dict[uuid.UUID, float] = {}
    rows: dict[uuid.UUID, tuple[KnowledgeChunk, KnowledgeSource]] = {}
    for weight, lst in zip(_weights(len(ranked), bool(designation)), ranked, strict=True):
        for rank, (chunk, src) in enumerate(lst):
            scores[chunk.id] = scores.get(chunk.id, 0.0) + weight / (RRF_K + rank + 1)
            rows[chunk.id] = (chunk, src)
    order = sorted(scores, key=scores.get, reverse=True)[: max(k * 2, k)]
    hits = []
    for cid in order:
        h = _hit(*rows[cid])
        h.score = round(scores[cid], 6)
        hits.append(h)
    hits = rerank(query, hits)[:k]
    for i, h in enumerate(hits, 1):
        h.n = i
    return hits


def _weights(n: int, has_reference: bool) -> list[float]:
    if has_reference:
        return [3.0] + [1.0] * (n - 1)
    return [1.0] * n


def rerank(query: str, hits: list[SearchHit]) -> list[SearchHit]:
    s = get_settings()
    if not s.rerank_url or len(hits) < 2:
        return hits
    try:
        r = httpx.post(
            s.rerank_url.rstrip("/") + "/rerank",
            json={"model": s.rerank_model, "query": query, "documents": [h.content for h in hits]},
            timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        order = sorted(results, key=lambda x: x.get("relevance_score", 0), reverse=True)
        return [hits[x["index"]] for x in order if 0 <= x.get("index", -1) < len(hits)] or hits
    except (httpx.HTTPError, ValueError, KeyError):
        return hits

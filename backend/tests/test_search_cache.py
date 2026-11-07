"""Смысловой поиск без pgvector: векторы держатся в памяти и обновляются, когда в базе появились новые фрагменты."""

from sqlalchemy import select


def test_vector_matrix_cached_and_refreshed(db, demo):
    from bis.models import KnowledgeChunk, KnowledgeSource
    from bis.rag import search as S

    chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.embedding.is_not(None)))
    dim = len(chunk.embedding)
    q = [float(x) for x in chunk.embedding]
    flt = dict(org_id=demo.id, kinds=None, include_global=True, only_active=False, source_ids=None)
    S.invalidate_vectors()

    first = S._semantic_in_memory(db, q, flt, 5)
    assert first and first[0][0].id == chunk.id  # самый близкий к своему же вектору — он сам
    ids, m = S._vectors(db, flt, dim)
    assert S._vectors(db, flt, dim)[1] is m  # второй раз — из памяти

    src = KnowledgeSource(organization_id=demo.id, title="Новый документ", kind="org_document", status="active")
    db.add(src)
    db.flush()
    db.add(KnowledgeChunk(organization_id=demo.id, source_id=src.id, ord=0, content="новый фрагмент", embedding=q))
    db.flush()
    ids2, m2 = S._vectors(db, flt, dim)
    assert m2 is not m and len(ids2) == len(ids) + 1  # база изменилась — матрица пересобрана

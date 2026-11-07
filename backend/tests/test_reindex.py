"""bis reindex при обновлении досчитывает только недостающие векторы; после смены модели — пересчитывает все."""

from sqlalchemy import select, update


def test_reindex_incremental(db, demo, tmp_path, monkeypatch):
    from bis.cli import main
    from bis.config import get_settings
    from bis.models import KnowledgeChunk
    from bis.rag.embeddings import HashingEmbedder, get_embedder, set_embedder

    monkeypatch.setattr(get_settings(), "storage_dir", tmp_path / "files")
    base = get_embedder()
    calls = []

    class Counting(HashingEmbedder):
        def embed_documents(self, texts):
            calls.append(len(texts))
            return base.embed_documents(texts)

    set_embedder(Counting(base.dim))
    total = len(db.scalars(select(KnowledgeChunk.id)).all())
    some = db.scalars(select(KnowledgeChunk.id).limit(3)).all()
    db.execute(update(KnowledgeChunk).where(KnowledgeChunk.id.in_(some)).values(embedding=None))
    db.commit()

    assert main(["reindex"]) == 0
    assert sum(calls) == 3  # только без векторов
    assert (tmp_path / ".embedder").exists()
    calls.clear()
    assert main(["reindex"]) == 0 and sum(calls) == 0  # всё уже есть — ни одного запроса к модели
    (tmp_path / ".embedder").write_text("OtherEmbedder:model:256", encoding="utf-8")
    assert main(["reindex"]) == 0 and sum(calls) == total  # модель сменилась — пересчёт всех

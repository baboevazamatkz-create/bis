from sqlalchemy import select

from bis.models import Document


def test_plain_document_search_does_not_initialize_models(client, auth, db, demo, monkeypatch):
    from bis.api import deps
    from bis.rag import embeddings

    doc = Document(organization_id=demo.id, kind="project_doc", title="Рабочий лист",
                   file_name="КЖ0-план_047.pdf", data={"library_scope": "organization"})
    db.add(doc)
    db.commit()

    def broken_gateway():
        raise AssertionError("Plain search must not initialize an LLM")

    def broken_embeddings():
        raise ValueError("Missing folder")

    monkeypatch.setattr(deps, "get_gateway", broken_gateway)
    monkeypatch.setattr(embeddings, "get_embedder", broken_embeddings)
    for query in ("кж0", "КЖ0-план_047.pdf"):
        response = client.get("/api/brain/search", headers=auth, params={"q": query, "workspace": "executive"})
        assert response.status_code == 200, response.text
        assert str(doc.id) in [d["id"] for d in response.json()]


def test_knowledge_search_survives_missing_embedding_configuration(client, auth, monkeypatch):
    from bis.rag import embeddings

    def broken_embeddings():
        raise ValueError("Missing embedding URL")

    monkeypatch.setattr(embeddings, "get_embedder", broken_embeddings)
    response = client.get("/api/knowledge/search", headers=auth, params={"q": "обратная засыпка"})
    assert response.status_code == 200, response.text
    assert response.json()["results"]


def test_knowledge_search_finds_short_codes_and_filenames(client, auth, db, demo):
    from bis.rag.embeddings import get_embedder
    from bis.rag.ingest import ingest_text

    src = ingest_text(db, get_embedder(), org_id=demo.id, text="Армирование фундаментной плиты.",
                      kind="project_doc", title="КЖ: рабочие листы")
    src.file_name = "RD-plan_047.xlsx"
    db.commit()
    for query in ("кж", "RD-plan_047.xlsx"):
        response = client.get("/api/knowledge/search", headers=auth, params={"q": query})
        assert response.status_code == 200, response.text
        assert str(src.id) in [hit["source_id"] for hit in response.json()["results"]]


def test_optional_vector_acceleration_failure_keeps_lexical_results(client, auth, monkeypatch):
    from bis.rag import search
    from bis.rag.embeddings import HashingEmbedder, set_embedder

    embedder = HashingEmbedder()
    embedder.semantic = True
    set_embedder(embedder)
    monkeypatch.setattr(search, "VECTOR", False)

    def unavailable(*args, **kwargs):
        raise ImportError("NumPy not installed")

    monkeypatch.setattr(search, "_semantic_in_memory", unavailable)
    response = client.get("/api/knowledge/search", headers=auth, params={"q": "обратная засыпка"})
    assert response.status_code == 200, response.text
    assert response.json()["results"]


def test_literal_search_still_respects_tenant_and_project_filters(client, auth, db, demo):
    from bis.models import ConstructionObject
    from bis.services.library_access import bind_context

    projects = list(db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id)))
    first = projects[0]
    second = ConstructionObject(organization_id=demo.id, name="Другой проект")
    db.add(second)
    db.flush()
    demo.settings = {**(demo.settings or {}), "libraries_strict": True}
    hidden = Document(organization_id=demo.id, object_id=second.id, kind="project_doc",
                      title="КЖ hidden", file_name="КЖ-hidden.pdf", data={})
    db.add(hidden)
    db.commit()
    bind_context(db, demo.id, first.id)
    response = client.get("/api/brain/search", headers={**auth, "X-BIS-Project": str(first.id)},
                          params={"q": "КЖ-hidden.pdf", "workspace": "executive"})
    assert response.status_code == 200, response.text
    assert str(hidden.id) not in [d["id"] for d in response.json()]

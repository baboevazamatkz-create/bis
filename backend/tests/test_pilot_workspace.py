"""Pilot regressions: real file formats, scoped rereading, lifecycle controls and selected-document answers."""

import io
import json
import os

import pytest
from docx import Document as Word
from openpyxl import Workbook
from sqlalchemy import func, select

from bis.config import get_settings
from bis.models import ConstructionObject, Document, KnowledgeChunk, KnowledgeSource
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_text
from bis.rag.parsers import parse_docx, parse_file, parse_xlsx
from bis.services import library_sync
from bis.services.storage import get_storage


def test_spreadsheet_preserves_columns_and_reports_uncached_formulas():
    wb = Workbook()
    ws = wb.active
    ws.append(["Материал", "Марка", "Количество"])
    ws.append(["Кабель", None, 125])
    ws.append(["Итого", None, "=SUM(C2:C2)"])
    out = io.BytesIO()
    wb.save(out)
    parsed = parse_xlsx(out.getvalue())
    assert "Кабель |  | 125" in parsed.text
    assert "Формула без сохранённого результата: =SUM(C2:C2)" in parsed.text
    assert parsed.meta["uncached_formulas"] == ["Sheet!C3"]


def test_docx_reads_headers_and_nested_tables_without_collapsing_columns():
    word = Word()
    word.sections[0].header.paragraphs[0].text = "Генподрядчик ООО Стройпроект"
    table = word.add_table(rows=1, cols=3)
    table.cell(0, 0).text = "Кабель"
    table.cell(0, 2).text = "125"
    nested = word.add_table(rows=1, cols=1).cell(0, 0).add_table(rows=1, cols=2)
    nested.cell(0, 0).text = "Гарантийный срок"
    nested.cell(0, 1).text = "60 месяцев"
    out = io.BytesIO()
    word.save(out)
    parsed = parse_docx(out.getvalue())
    assert "Кабель |  | 125" in parsed.text
    assert "Генподрядчик ООО Стройпроект" in parsed.text
    assert "Гарантийный срок | 60 месяцев" in parsed.text


def test_windows_unicode_text_is_decoded():
    parsed = parse_file("Рабочая документация. Кабель 125 метров.".encode("utf-16"), "спецификация.txt")
    assert "Кабель 125 метров" in parsed.text and "\x00" not in parsed.text


def test_embedding_failure_preserves_searchable_document_text(db, demo):
    class Broken:
        semantic = True

        def embed_documents(self, texts):
            raise RuntimeError("embedding service unavailable")

    source = ingest_text(
        db,
        Broken(),
        org_id=demo.id,
        text="Маркер сохранности лексического индекса",
        title="Документ",
        kind="org_document",
    )
    assert source.chunk_count and source.meta["embedding_status"] == "error"
    assert db.scalar(select(KnowledgeChunk.content).where(KnowledgeChunk.source_id == source.id))


@pytest.fixture
def folder(db, demo, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "library_roots", [tmp_path])
    root = tmp_path / "project"
    root.mkdir()
    obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    c = LibraryConnection(
        organization_id=demo.id, object_id=obj.id, level="project", path=str(root), name="Проект"
    )
    db.add(c)
    db.commit()
    return c, root, obj


def test_existing_acts_are_not_treated_as_blank_forms(db, demo, folder):
    c, root, _ = folder
    (root / "АОСР 17.txt").write_text(
        "АКТ освидетельствования скрытых работ № 17\nАрмирование плиты выполнено.", encoding="utf-8"
    )
    library_sync.scan(db, get_storage(), get_embedder(), None, c)
    file = db.scalar(select(LibraryFile).where(LibraryFile.connection_id == c.id))
    version = db.get(LibraryVersion, file.active_version_id)
    assert version.form_kind is None
    assert db.get(Document, version.document_id).kind == "aosr"


def test_force_rereads_unchanged_files(client, auth, db, folder):
    c, root, obj = folder
    (root / "Рабочая документация.txt").write_text(
        "Рабочая документация\nНа участке 123 предусмотрен кабель 125 метров.", encoding="utf-8"
    )
    library_sync.scan(db, get_storage(), get_embedder(), None, c)
    db.commit()
    first = db.scalar(select(LibraryFile.active_version_id).where(LibraryFile.connection_id == c.id))
    response = client.post(
        f"/api/libraries/{c.id}/sync?force=true", headers={**auth, "X-BIS-Project": str(obj.id)}
    )
    assert response.status_code == 202, response.text
    db.expire_all()
    second = db.scalar(select(LibraryFile.active_version_id).where(LibraryFile.connection_id == c.id))
    assert first != second
    assert (
        db.scalar(
            select(func.count())
            .select_from(LibraryVersion)
            .join(LibraryFile, LibraryFile.id == LibraryVersion.file_id)
            .where(LibraryFile.connection_id == c.id)
        )
        == 2
    )


def test_clear_connection_preserves_originals_and_generated_acts(client, auth, db, demo, folder):
    c, root, obj = folder
    original = root / "Рабочая документация.txt"
    original.write_text("Рабочая документация\nКабель на участке 123.", encoding="utf-8")
    library_sync.scan(db, get_storage(), get_embedder(), None, c)
    generated = Document(
        organization_id=demo.id,
        object_id=obj.id,
        kind="aosr",
        title="Созданный акт",
        source="generated",
        data={},
    )
    db.add(generated)
    db.commit()
    generated_id = generated.id
    response = client.delete(
        f"/api/libraries/{c.id}/contents", headers={**auth, "X-BIS-Project": str(obj.id)}
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert original.is_file() and db.get(Document, generated_id)
    assert db.get(LibraryConnection, c.id).enabled is False
    assert not db.scalar(select(LibraryFile.id).where(LibraryFile.connection_id == c.id))


def test_clear_refuses_a_running_folder(client, auth, folder):
    c, _, obj = folder
    with library_sync.connection_lease(c.id) as acquired:
        assert acquired
        response = client.delete(
            f"/api/libraries/{c.id}/contents", headers={**auth, "X-BIS-Project": str(obj.id)}
        )
        assert response.status_code == 409


def uploaded(db, demo, obj, text="Рабочая документация. Срок поставки кабеля — 17 ноября 2026 года."):
    key = get_storage().put(text.encode(), "original.txt", prefix="pilot-regressions")
    doc = Document(
        organization_id=demo.id,
        object_id=obj.id,
        kind="other",
        title="Оригинал",
        source="uploaded",
        file_key=key,
        file_name="original.txt",
        mime="text/plain",
        data={},
        status="registered",
    )
    db.add(doc)
    db.flush()
    ingest_text(
        db,
        get_embedder(),
        org_id=demo.id,
        title=doc.title,
        text="Старый неполный индекс",
        kind="org_document",
        document_id=doc.id,
    )
    db.commit()
    return doc


def test_old_uploads_can_be_reread_from_stored_originals(client, auth, db, demo):
    obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    doc = uploaded(db, demo, obj)
    key = doc.file_key
    doc_id = doc.id
    response = client.post(
        "/api/workspace/imports/reread?level=project", headers={**auth, "X-BIS-Project": str(obj.id)}
    )
    assert response.status_code == 202, response.text
    db.expire_all()
    doc = db.get(Document, doc_id)
    assert doc.file_key == key and doc.data["read_state"] == "ready"
    text = "\n".join(
        db.scalars(
            select(KnowledgeChunk.content)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(KnowledgeSource.document_id == doc_id)
        )
    )
    assert "17 ноября 2026" in text and "Старый неполный" not in text


def test_reread_failure_keeps_original_and_old_index(db, demo, monkeypatch):
    from bis.services import archive, source_reread

    obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    doc = uploaded(db, demo, obj)
    ids = source_reread.queue_documents(db, [doc])

    def fail(*args):
        raise ValueError("OCR: нет доступа")

    monkeypatch.setattr(archive, "_pages", fail)
    source_reread.process_many(ids)
    db.expire_all()
    assert db.get(Document, doc.id).data["read_state"] == "error"
    assert "OCR: нет доступа" in db.get(Document, doc.id).data["reading_error"]
    assert db.scalar(
        select(KnowledgeChunk.id).join(KnowledgeSource).where(KnowledgeSource.document_id == doc.id)
    )


def test_workspace_lists_are_project_scoped(client, auth, db, demo):
    first = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    second = ConstructionObject(organization_id=demo.id, name="Другой проект")
    db.add(second)
    db.flush()
    visible = uploaded(db, demo, first)
    hidden = uploaded(db, demo, second)
    r = client.get(
        "/api/workspace/documents?category=project", headers={**auth, "X-BIS-Project": str(first.id)}
    )
    assert r.status_code == 200, r.text
    assert str(visible.id) in [d["id"] for d in r.json()["documents"]]
    assert str(hidden.id) not in [d["id"] for d in r.json()["documents"]]


def test_selected_contract_answers_read_that_document(client, auth, db, demo):
    from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
    from bis.llm.providers.offline import ScriptedProvider

    obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    doc = uploaded(
        db,
        demo,
        obj,
        "ДОГОВОР СУБПОДРЯДА № 893. Предмет договора: монтаж.\nСрок окончания работ — 17 ноября 2026 года.",
    )
    doc.kind = "contract"
    db.commit()
    from bis.services import source_reread

    source_reread.process_many(source_reread.queue_documents(db, [doc]))
    db.expire_all()
    llm = ScriptedProvider(["В договоре указан срок 17 ноября 2026 года."])
    set_gateway(LLMGateway(entries={"test": ProviderEntry(llm, "confidential")}))
    result = client.post(
        "/api/chat",
        headers={**auth, "X-BIS-Project": str(obj.id)},
        json={
            "message": "Какой срок окончания работ по договору?",
            "workspace": "executive",
            "document_id": str(doc.id),
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["provider"] != "project_facts"
    prompt = "\n".join(m.content for m in llm.calls[0]["messages"])
    assert "17 ноября 2026" in prompt and str(doc.id) in prompt
    assert any(
        t["tool"] == "get_document" and t["arguments"].get("document_id") == str(doc.id)
        for t in result.json()["trace"]
    )


@pytest.fixture
def local_settings(tmp_path, monkeypatch):
    keys = [
        "BIS_LLM_CONFIG_FILE",
        "YANDEX_FOLDER_ID",
        "BIS_YANDEX_FOLDER_ID",
        "YANDEX_API_KEY",
        "BIS_OCR_PROVIDER",
        "BIS_STT_PROVIDER",
        "BIS_LIBRARY_ROOTS",
    ]
    before = {key: os.environ.get(key) for key in keys}
    (tmp_path / ".env").write_text("UNRELATED=preserve\n", encoding="utf-8")
    monkeypatch.setenv("BIS_LOCAL_DIR", str(tmp_path))
    yield tmp_path
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def test_local_ai_setup_never_returns_the_key_and_preserves_other_settings(client, auth, local_settings):
    secret = "fixture-secret-for-ai-only"
    r = client.post(
        "/api/workspace/ai-setup", headers=auth, json={"folder_id": "fixturefolder", "api_key": secret}
    )
    assert r.status_code == 200, r.text
    assert secret not in r.text
    assert "UNRELATED=preserve" in (local_settings / ".env").read_text()
    status = client.get("/api/workspace/reading-status", headers=auth)
    assert secret not in status.text and status.json()["ocr_configured"] is True
    assert get_settings().yandex() == ("fixturefolder", secret)


def test_clear_uploaded_sources_does_not_clear_other_projects_or_generated_acts(client, auth, db, demo):
    first = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    second = ConstructionObject(organization_id=demo.id, name="Второй объект")
    db.add(second)
    db.flush()
    removed = uploaded(db, demo, first)
    preserved = uploaded(db, demo, second)
    generated = Document(
        organization_id=demo.id,
        object_id=first.id,
        kind="aosr",
        source="generated",
        title="Готовый акт",
        data={},
    )
    db.add(generated)
    db.commit()
    ids = removed.id, preserved.id, generated.id
    r = client.delete(
        "/api/workspace/imports?level=project", headers={**auth, "X-BIS-Project": str(first.id)}
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Document, ids[0]) is None
    assert db.get(Document, ids[1]) and db.get(Document, ids[2])
    assert db.scalar(select(KnowledgeSource.id).where(KnowledgeSource.document_id == ids[0])) is None


def test_folder_progress_publishes_each_file(db, demo, folder):
    from bis.db import SessionLocal

    c, root, _ = folder
    for name in ("Первый", "Второй"):
        (root / f"{name}.txt").write_text("Рабочая документация. Кабель на участке 123.", encoding="utf-8")
    visible = []

    def checkpoint():
        db.commit()
        with SessionLocal() as observer:
            visible.append(
                observer.scalar(
                    select(func.count())
                    .select_from(LibraryFile)
                    .where(LibraryFile.connection_id == c.id, LibraryFile.state == "ready")
                )
            )

    library_sync.scan(db, get_storage(), get_embedder(), None, c, checkpoint=checkpoint)
    assert visible == [1, 2]


def test_all_tiff_pages_reach_ocr(monkeypatch):
    import httpx
    from PIL import Image

    from bis import ocr
    from bis.services import archive

    first, second = Image.new("RGB", (20, 20), "white"), Image.new("RGB", (20, 20), "black")
    out = io.BytesIO()
    first.save(out, format="TIFF", save_all=True, append_images=[second])
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"result": {"textAnnotation": {"fullText": f"Страница {len(calls)}"}}}
        )

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(type(get_settings()), 'yandex', lambda self: ('fixturefolder', 'fixture-key'))
    monkeypatch.setattr(ocr, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    pages, method = archive._raw_pages(out.getvalue(), "scan.tiff", "image/tiff")
    assert method == "ocr" and len(pages) == 2 and len(calls) == 2
    assert [p.number for p in pages] == [1, 2]


def test_local_connection_can_use_an_explicitly_selected_folder(
    client, auth, db, demo, local_settings, monkeypatch
):
    root = local_settings / "documents"
    root.mkdir()
    (root / "Рабочая документация.txt").write_text(
        "Рабочая документация. Кабель 125 метров.", encoding="utf-8"
    )
    monkeypatch.setattr(get_settings(), "library_roots", [])
    obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    r = client.post(
        "/api/libraries",
        headers=auth,
        json={"name": "Проект", "path": str(root), "level": "project", "object_id": str(obj.id)},
    )
    assert r.status_code == 201, r.text
    assert root in get_settings().library_roots
    assert "UNRELATED=preserve" in (local_settings / ".env").read_text()


def test_local_settings_file_beats_stale_environment_but_keeps_current_ui(tmp_path, monkeypatch):
    from bis.local import load_env

    path = tmp_path / ".env"
    path.write_text(
        "YANDEX_FOLDER_ID=new-folder\nBIS_OCR_PROVIDER=yandex\nBIS_WEB_DIR=old-web\n", encoding="utf-8"
    )
    monkeypatch.setenv("YANDEX_FOLDER_ID", "old-folder")
    monkeypatch.setenv("BIS_OCR_PROVIDER", "none")
    monkeypatch.setenv("BIS_WEB_DIR", "new-versioned-web")
    load_env(path, prefer_file=True)
    assert os.environ["YANDEX_FOLDER_ID"] == "new-folder"
    assert os.environ["BIS_OCR_PROVIDER"] == "yandex"
    assert os.environ["BIS_WEB_DIR"] == "new-versioned-web"

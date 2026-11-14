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

    continued = client.post('/api/chat', headers={**auth, 'X-BIS-Project': str(obj.id)},
                            json={'message': 'Уточни срок окончания', 'workspace': 'executive', 'session_id': result.json()['session_id']})
    assert continued.status_code == 200, continued.text
    assert any(t['tool'] == 'get_document' and t['arguments'].get('document_id') == str(doc.id)
               for t in continued.json()['trace'])


@pytest.fixture
def local_settings(tmp_path, monkeypatch):
    keys = [
        "BIS_LLM_CONFIG_FILE",
        "BIS_ANTHROPIC_API_KEY",
        "BIS_OPENAI_API_KEY",
        "BIS_COMPATIBLE_API_KEY",
        "BIS_GIGACHAT_API_KEY",
        "YANDEX_FOLDER_ID",
        "BIS_YANDEX_FOLDER_ID",
        "YANDEX_API_KEY",
        "BIS_OCR_PROVIDER",
        "BIS_STT_PROVIDER",
        "BIS_YANDEX_API_KEY_ENV",
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


def test_anthropic_can_be_selected_in_bis_without_changing_yandex_ocr(client, auth, local_settings):
    yandex_key = 'fixture-yandex-ocr-key'
    assert client.post('/api/workspace/ai-setup', headers=auth,
                       json={'folder_id': 'ocrfolder', 'api_key': yandex_key}).status_code == 200
    secret = 'sk-ant-fixture-key'
    response = client.post('/api/workspace/ai-provider', headers=auth, json={
        'provider': 'anthropic', 'model': 'claude-opus-5-5', 'api_key': secret,
    })
    assert response.status_code == 200, response.text
    assert secret not in response.text
    settings = get_settings()
    providers = {p.name: p for p in settings.llm_providers()}
    assert providers['anthropic-claude'].kind == 'anthropic'
    assert providers['anthropic-claude'].model == 'claude-opus-5-5'
    assert providers['anthropic-claude'].resolved_api_key() == secret
    assert settings.llm_routes()['default'] == ['anthropic-claude', 'offline']
    from bis.llm.gateway import get_gateway
    assert get_gateway().entries['anthropic-claude'].provider.model == 'claude-opus-5-5'
    assert settings.yandex() == ('ocrfolder', yandex_key)
    status = client.get('/api/workspace/reading-status', headers=auth)
    assert status.status_code == 200 and secret not in status.text
    assert status.json()['selected_provider'] == 'anthropic-claude'
    assert status.json()['provider_key_configured'] is True
    assert status.json()['ocr_configured'] is True
    assert 'UNRELATED=preserve' in (local_settings / '.env').read_text()


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
        assert calls[-1]["mimeType"] == "image/jpeg"
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


def test_browser_upload_reads_files_and_rejects_traversal(client, auth, db, demo, folder):
    _, root, obj = folder
    headers = {**auth, 'X-BIS-Project': str(obj.id)}
    response = client.post('/api/libraries/upload', headers=headers, data={'level': 'project', 'object_id': str(obj.id)},
        files=[('files', ('folder/contract.txt', 'Договор субподряда. Срок 17 ноября 2026'.encode(), 'text/plain'))])
    assert response.status_code == 201, response.text
    connection = response.json()
    rows = client.get(f"/api/libraries/{connection['id']}/files", headers=headers).json()
    assert len(rows) == 1 and rows[0]['state'] == 'ready'
    bad = client.post('/api/libraries/upload', headers=headers, data={'level': 'project', 'object_id': str(obj.id)},
        files=[('files', ('../escape.txt', b'forbidden', 'text/plain'))])
    assert bad.status_code == 400
    assert not (root.parent / 'escape.txt').exists()


def test_local_admin_can_connect_knowledge_but_remote_admin_cannot(client, auth, folder, monkeypatch, db):
    from bis.models import User

    for user in db.scalars(select(User)):
        user.is_platform_admin = False
    db.commit()
    _, root, _ = folder
    payload = {'name': 'Знания', 'path': str(root), 'level': 'knowledge'}
    monkeypatch.delenv('BIS_LOCAL_DIR', raising=False)
    assert client.post('/api/libraries', headers=auth, json=payload).status_code == 403
    monkeypatch.setenv('BIS_LOCAL_DIR', str(root))
    assert client.get('/api/libraries', headers=auth).json()['platform_admin']
    assert client.post('/api/libraries', headers=auth, json=payload).status_code == 201


def test_rtf_disguised_as_doc_reads_russian():
    data = br"{\rtf1\ansi\ansicpg1251 \'c4\'ee\'e3\'ee\'e2\'ee\'f0 123}"
    assert 'Договор 123' in parse_file(data, 'договор.doc').text


def test_doc_piece_table_preserves_unicode_and_cells():
    import struct

    from bis.rag.legacy_word import read_pieces

    text = 'Договор № 123\rКабель\x07125 м\r'
    word = bytearray(1024)
    struct.pack_into('<HH', word, 0, 0xA5EC, 0xC1)
    struct.pack_into('<H', word, 32, 14)
    struct.pack_into('<H', word, 62, 22)
    struct.pack_into('<H', word, 152, 93)
    raw = text.encode('utf-16-le')
    word[700:700 + len(raw)] = raw
    plc = struct.pack('<IIHIH', 0, len(text), 0, 700, 0)
    table = b'\x02' + struct.pack('<I', len(plc)) + plc
    struct.pack_into('<II', word, 154 + 33 * 8, 0, len(table))

    class Package:
        def openstream(self, name):
            assert name == '0Table'
            return io.BytesIO(table)

    assert read_pieces(word, Package()) == 'Договор № 123\nКабель | 125 м'


def test_ai_settings_survive_repeated_saves_and_fresh_process(client, auth, local_settings, monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv('BIS_YANDEX_API_KEY_ENV', 'OLD_UNUSED_KEY')
    get_settings.cache_clear()
    first = client.post('/api/workspace/ai-setup', headers=auth,
                        json={'folder_id': 'folderone', 'api_key': 'fixture-persistent-key'})
    assert first.status_code == 200
    original_config = get_settings().llm_config_file
    original_content = original_config.read_bytes()
    for _ in range(3):
        response = client.post('/api/workspace/ai-setup', headers=auth, json={'folder_id': 'foldertwo'})
        assert response.status_code == 200, response.text
        status = client.get('/api/workspace/reading-status', headers=auth).json()
        assert status['key_configured'] and status['text_configured'] and status['ocr_configured']
    assert original_config.read_bytes() == original_content
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('BIS_', 'YANDEX_')):
            env.pop(key)
    script = '''
from pathlib import Path
from bis.local import load_env
load_env(Path('.env'), prefer_file=True)
from bis.config import get_settings
from bis.llm.gateway import get_gateway
s=get_settings()
assert s.yandex()==('foldertwo','fixture-persistent-key')
assert get_gateway().entries['yandexgpt'].provider.client.headers['Authorization']=='Api-Key fixture-persistent-key'
assert get_gateway().has_capability('text')==['yandexgpt']
from bis import ocr
assert ocr.available()
print('restart OK')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=local_settings, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'restart OK'


def test_gateway_build_cannot_overwrite_new_ai_settings(client, auth, local_settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from bis.api.workspace import AISetup, ai_setup
    from bis.llm.gateway import LLMGateway, get_gateway, set_gateway

    ai_setup(AISetup(folder_id='oldfolder', api_key='fixture-old-key'), None)
    set_gateway(None)
    entered, release = Event(), Event()
    original = LLMGateway.from_settings

    def delayed(cls, settings):
        entered.set()
        assert release.wait(5)
        return original(settings)

    monkeypatch.setattr(LLMGateway, 'from_settings', classmethod(delayed))
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(get_gateway)
        assert entered.wait(5)
        saved = pool.submit(ai_setup, AISetup(folder_id='newfolder', api_key='fixture-new-key'), None)
        release.set()
        old.result(timeout=10)
        saved.result(timeout=10)
    gateway = get_gateway()
    assert gateway.entries['yandexgpt'].provider.model.startswith('gpt://newfolder/')
    assert gateway.entries['yandexgpt'].provider.client.headers['Authorization'] == 'Api-Key fixture-new-key'


def test_failed_save_retains_previous_configuration(client, auth, local_settings, monkeypatch):
    from bis.api.workspace import AISetup, ai_setup
    from bis.llm.gateway import get_gateway
    from bis.services import local_config

    ai_setup(AISetup(folder_id='oldfolder', api_key='fixture-old-key'), None)
    before = (local_settings / '.env').read_bytes()
    gateway = get_gateway()

    def fail(values):
        raise PermissionError('fixture locked file')

    monkeypatch.setattr(local_config, 'save', fail)
    with pytest.raises(PermissionError):
        ai_setup(AISetup(folder_id='newfolder', api_key='fixture-new-key'), None)
    assert (local_settings / '.env').read_bytes() == before
    assert get_gateway() is gateway
    assert get_settings().yandex() == ('oldfolder', 'fixture-old-key')

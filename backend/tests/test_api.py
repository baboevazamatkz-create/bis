"""Сквозные тесты API на PostgreSQL: авторизация, реестры, база знаний, документы, чат с агентами."""

from __future__ import annotations

import io

from conftest import login

from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall


def _object_id(client, auth) -> str:
    objs = client.get("/api/objects", headers=auth).json()
    assert objs
    return objs[0]["id"]


def test_login_and_me(client, auth):
    me = client.get("/api/auth/me", headers=auth).json()
    assert me["user"]["email"] == "admin@demo.bis"
    assert client.post("/api/auth/login", json={"email": "admin@demo.bis", "password": "bad"}).status_code == 401
    assert client.get("/api/objects").status_code == 401


def test_registry_crud_and_tenant_isolation(client, auth, db):
    from bis.demo import create_organization

    r = client.post("/api/counterparties", headers=auth, json={"name": "ООО «Поставщик»", "kind": "supplier", "inn": "7705000007"})
    assert r.status_code == 201
    cp_id = r.json()["id"]
    assert client.patch(f"/api/counterparties/{cp_id}", headers=auth, json={"phone": "+7 495 1"}).json()["phone"] == "+7 495 1"
    assert any(c["id"] == cp_id for c in client.get("/api/counterparties?q=Поставщик", headers=auth).json())

    create_organization(db, name="ООО «Чужая»", admin_email="other@x.ru", admin_password="password1", admin_name="Другой")
    db.commit()
    other = login(client, "other@x.ru", "password1")
    assert client.get(f"/api/counterparties/{cp_id}", headers=other).status_code == 404
    assert client.get("/api/objects", headers=other).json() == []
    # нельзя сослаться на чужую запись
    r = client.post("/api/contracts", headers=other, json={"kind": "supply", "number": "1", "counterparty_id": cp_id})
    assert r.status_code == 400


def test_role_permissions(client, auth):
    viewer = client.post("/api/users", headers=auth, json={"email": "v@demo.bis", "full_name": "Наблюдатель",
                                                           "role": "viewer", "password": "viewer123"})
    assert viewer.status_code == 201
    h = login(client, "v@demo.bis", "viewer123")
    assert client.get("/api/objects", headers=h).status_code == 200
    assert client.post("/api/objects", headers=h, json={"name": "X"}).status_code == 403


def test_knowledge_upload_and_search(client, auth):
    text = "ДЕМО-ГОСТ\n1 Общие положения\n1.1 Сварные швы контролируют визуально и измерительно на всей длине шва.\n"
    r = client.post("/api/knowledge/upload", headers=auth, data={"kind": "org_document", "title": "Регламент сварки"},
                    files={"file": ("reg.txt", text.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    assert r.json()["chunk_count"] >= 1
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "контроль сварных швов"}).json()["results"]
    assert res[0]["title"] == "Регламент сварки"
    res = client.get("/api/knowledge/search", headers=auth,
                     params={"q": "ДЕМО-СП 999.13330.2026 п. 3.1.2", "scope": "norms"}).json()["results"]
    assert res[0]["citation"] == "ДЕМО-СП 999.13330.2026, п. 3.1.2"
    # общую библиотеку меняет только администратор платформы
    src = next(s for s in client.get("/api/knowledge/sources", headers=auth).json() if s["is_global"])
    pto = login(client, "pto@demo.bis", "demo12345")
    assert client.patch(f"/api/knowledge/sources/{src['id']}", headers=pto, json={"status": "cancelled"}).status_code == 403
    assert client.patch(f"/api/knowledge/sources/{src['id']}", headers=auth, json={"status": "cancelled"}).status_code == 200
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "обратная засыпка", "scope": "norms"}).json()
    assert "отменён" in res["results"][0]["citation"]


def test_aosr_form_flow(client, auth):
    obj = _object_id(client, auth)
    body = {"object_id": obj, "works": "Обратная засыпка пазух котлована в осях 1-5", "project_docs": "ЖК-01-КЖ0",
            "materials": ["Песок средний, паспорт № 77 от 01.05.2026"], "executive_docs": ["Исполнительная схема № 3"],
            "work_start": "2026-05-01", "work_end": "2026-05-05", "norms": ["ДЕМО-СП 999.13330.2026, п. 3.1.2"],
            "next_works": "Устройство отмостки", "act_date": "2026-05-06"}
    check = client.post("/api/documents/aosr/check", headers=auth, json=body).json()
    assert [i for i in check["issues"] if i["level"] == "error"] == []
    assert "developer_control" in check["representatives"]
    r = client.post("/api/documents/aosr", headers=auth, json=body)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["number"] == "1" and doc["status"] == "draft"
    f = client.get(f"/api/documents/{doc['id']}/file", headers=auth)
    assert f.status_code == 200 and f.content[:2] == b"PK"
    # второй акт по объекту получает следующий номер
    assert client.post("/api/documents/aosr", headers=auth, json=body).json()["number"] == "2"
    assert client.post(f"/api/documents/{doc['id']}/approve", headers=auth).json()["status"] == "approved"


def test_reject_creates_feedback_memory(client, auth, db):
    from bis.models import Document

    obj = _object_id(client, auth)
    doc = client.post("/api/documents/letter", headers=auth, json={
        "addressee": ["Генеральному директору", "АО «Заказчик-Девелопмент»"], "subject": "О сроках",
        "body": ["Текст"], "object_id": obj}).json()
    row = db.get(Document, doc["id"])
    row.created_by = "agent:secretary"
    db.commit()
    r = client.post(f"/api/documents/{doc['id']}/reject", headers=auth, json={"reason": "Указывать номер договора"})
    assert r.json()["status"] == "rejected"
    memory = client.get("/api/memory", headers=auth).json()
    assert any(m["scope"] == "secretary" and "номер договора" in m["content"] for m in memory)


def test_upload_incoming_document_is_indexed(client, auth):
    r = client.post("/api/documents/upload", headers=auth, data={"kind": "incoming_letter", "title": "Письмо заказчика о графике"},
                    files={"file": ("in.txt", "Просим ускорить монтаж каркаса до 15 июля.".encode(), "text/plain")})
    assert r.status_code == 201 and r.json()["status"] == "registered"
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "монтаж каркаса", "scope": "organization"}).json()
    assert res["results"][0]["title"] == "Письмо заказчика о графике"


def test_chat_offline_norms_agent(client, auth):
    r = client.post("/api/chat", headers=auth, json={"message": "Какая толщина слоя обратной засыпки по СП?"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["agent"] == "norms"
    assert data["citations"] and "ДЕМО-СП 999.13330.2026" in data["citations"][0]["citation"]
    assert "офлайн" in data["text"]
    hist = client.get(f"/api/chat/sessions/{data['session_id']}", headers=auth).json()
    assert [m["role"] for m in hist] == ["user", "assistant"]


def test_chat_pto_agent_creates_aosr_with_scripted_llm(client, auth):
    obj = _object_id(client, auth)
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("1", "find_objects", {"query": "ЖК-01"})]),
        Message("assistant", tool_calls=[ToolCall("2", "create_aosr_draft", {
            "object_id": obj, "works": "Армирование плиты Пм1", "project_docs": "ЖК-01-КЖ1 л. 3",
            "materials": ["Арматура А500С, сертификат № 1"], "executive_docs": ["ИС № 7"],
            "work_start": "2026-05-10", "work_end": "2026-05-12", "norms": ["ДЕМО-СП 999.13330.2026, п. 4.2.3"],
            "next_works": "Бетонирование плиты Пм1", "act_date": "2026-05-12"})]),
        "Черновик АОСР создан, замечаний нет.",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    data = client.post("/api/chat", headers=auth, json={"message": "Сделай АОСР на армирование плиты Пм1"}).json()
    assert data["agent"] == "pto"
    assert data["documents"] and data["documents"][0]["kind"] == "aosr"
    assert [t["tool"] for t in data["trace"]] == ["find_objects", "create_aosr_draft"]
    # системный промпт содержит организацию и правила
    system = llm.calls[0]["messages"][0].content
    assert "ООО «СтройДемо»" in system
    # результат инструмента вернулся модели
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.role == "tool"]
    assert "ЖК-01" in tool_msgs[0].content
    docs = client.get("/api/documents?kind=aosr", headers=auth).json()
    assert docs[0]["created_by"] == "agent:pto"
    audit = client.get("/api/audit", headers=auth).json()
    assert any(a["action"] == "tool.create_aosr_draft" for a in audit)


def test_viewer_agent_cannot_create_documents(client, auth):
    client.post("/api/users", headers=auth, json={"email": "v2@demo.bis", "full_name": "Наблюдатель", "role": "viewer",
                                                  "password": "viewer123"})
    h = login(client, "v2@demo.bis", "viewer123")
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("1", "create_letter_draft", {"addressee": ["A"], "subject": "О", "body": ["т"]})]),
        "Не удалось",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    data = client.post("/api/chat", headers=h, json={"message": "Подготовь письмо заказчику", "agent": "secretary"}).json()
    assert data["documents"] == []
    assert data["trace"][0]["error"] is True


def test_tools_and_agents_listing(client, auth):
    tools = client.get("/api/tools", headers=auth).json()["tools"]
    assert {"search_knowledge", "create_aosr_draft"} <= {t["name"] for t in tools}
    assert all("inputSchema" in t for t in tools)
    agents = {a["name"] for a in client.get("/api/agents", headers=auth).json()}
    assert {"norms", "secretary", "pto", "assistant"} <= agents
    health = client.get("/api/health").json()
    assert health["status"] == "ok"


def test_org_template_used_for_letters(client, auth):
    import docx

    tpl = docx.Document()
    tpl.add_paragraph("БЛАНК {{ sender.name }} № {{ number }}: {{ subject }}")
    buf = io.BytesIO()
    tpl.save(buf)
    r = client.post("/api/templates", headers=auth, data={"kind": "letter", "name": "Бланк"},
                    files={"file": ("blank.docx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 201
    doc = client.post("/api/documents/letter", headers=auth, json={"addressee": ["Кому"], "subject": "О тесте",
                                                                   "body": ["Текст"]}).json()
    content = client.get(f"/api/documents/{doc['id']}/file", headers=auth).content
    from bis.documents.common import docx_text

    assert "БЛАНК ООО «СтройДемо» № 1/" in docx_text(content)

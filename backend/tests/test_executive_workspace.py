"""The PTO workspace keeps its documents, actions, and conversations separate."""

from sqlalchemy import select

from bis.agents.base import AgentContext
from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.models import Document, User
from bis.modules.executive import DOCUMENT_KINDS, WRITE_TOOLS
from bis.plugins.sdk import discover
from bis.services.storage import get_storage


def test_executive_agents_cannot_write_finance_or_material_records(db, demo):
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == "admin"))
    ctx = AgentContext(db=db, org_id=demo.id, user=user, gateway=None, embedder=None,
                       storage=get_storage(), workspace="executive")
    reg = discover()
    for agent in reg.agents.values():
        tools = agent.available_tools(ctx, reg.tools)
        writes = {t.name for t in tools if any(s.endswith(":write") for s in t.scopes)}
        assert writes <= WRITE_TOOLS
        assert not {"m29_report", "material_balance", "closing_check", "calc_penalty"} & {t.name for t in tools}


def test_executive_delegation_stays_in_workspace(db, demo):
    from bis.agents.tools_control import delegate_to_agent

    user = db.scalar(select(User).where(User.organization_id == demo.id))
    ctx = AgentContext(db=db, org_id=demo.id, user=user, gateway=None, embedder=None,
                       storage=get_storage(), workspace="executive")
    raw, error = delegate_to_agent.run(ctx, {"agent": "materials", "task": "Сделай списание"})
    assert error and "другому блоку" in raw


def test_executive_chat_filters_agents_and_separates_session_history(client, auth):
    llm = ScriptedProvider(["Помогаю с исполнительной документацией."])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    rejected = client.post("/api/chat", headers=auth, json={"message": "Привет", "workspace": "executive", "agent": "contracts"})
    assert rejected.status_code == 400
    result = client.post("/api/chat", headers=auth, json={"message": "Помоги проверить ИД", "workspace": "executive"})
    assert result.status_code == 200
    data = result.json()
    assert data["agent"] == "pto"
    assert "Рабочее пространство: исполнительная документация" in llm.calls[0]["messages"][0].content
    session_id = data["session_id"]
    assert session_id in {s["id"] for s in client.get("/api/chat/sessions?workspace=executive", headers=auth).json()}
    assert session_id not in {s["id"] for s in client.get("/api/chat/sessions", headers=auth).json()}
    assert client.get(f"/api/chat/sessions/{session_id}", headers=auth).status_code == 404
    assert client.get(f"/api/chat/sessions/{session_id}?workspace=executive", headers=auth).status_code == 200
    assert client.post("/api/chat", headers=auth, json={"message": "Продолжай", "session_id": session_id}).status_code == 404


def test_document_scope_is_filtered_on_server_before_pagination(client, auth, db, demo):
    for kind in ("aook", "m29", "ks2", "writeoff"):
        db.add(Document(organization_id=demo.id, kind=kind, title=f"Документ {kind}", status="draft",
                        source="generated", data={}, issues=[], created_by="test"))
    db.commit()
    docs = client.get("/api/documents?workspace=executive&limit=1", headers=auth).json()
    assert docs and all(d["kind"] in DOCUMENT_KINDS for d in docs)
    assert client.get("/api/documents?workspace=executive&kind=ks2", headers=auth).json() == []
    sources = client.get("/api/documents?workspace=executive&collection=sources", headers=auth).json()
    assert all(d["kind"] not in ("ks2", "m29", "aook") for d in sources)
    assert any(d["kind"] == "ks2" for d in client.get("/api/documents", headers=auth).json())
    found = client.get("/api/brain/search?workspace=executive", headers=auth).json()
    assert all(d["kind"] not in ("ks2", "m29", "writeoff") for d in found)


def test_viewer_cannot_export_journal_through_read_tool(db, demo):
    from bis.agents.tools_id import work_journal_tool

    user = db.scalar(select(User).where(User.organization_id == demo.id))
    user.role = "viewer"
    ctx = AgentContext(db=db, org_id=demo.id, user=user, gateway=None, embedder=None,
                       storage=get_storage(), workspace="executive")
    raw, error = work_journal_tool.run(ctx, {"period_start": "2026-01-01", "period_end": "2026-12-31", "create_document": True})
    assert error and "documents:write" in raw

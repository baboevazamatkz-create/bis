"""Очистка данных организации: всё накопленное удаляется, пользователи и реквизиты остаются, чужие данные не трогаются."""

from sqlalchemy import func, select


def test_reset_org_keeps_users_and_other_orgs(client, auth, db, demo):
    from conftest import login

    from bis.demo import create_organization
    from bis.models import ChatMessage, ConstructionObject, Document, KnowledgeChunk, Organization, User

    other, _ = create_organization(db, name="ООО «Другая»", inn="7700000077", admin_email="o@o.ru",
                                   admin_password="12345678", admin_name="О")
    db.commit()
    other_auth = login(client, "o@o.ru", "12345678")
    client.post("/api/objects", headers=other_auth, json={"name": "Чужой объект"})
    client.post("/api/chat", headers=auth, json={"message": "Какая допустимая толщина слоя обратной засыпки?"})
    count = lambda m, org: db.scalar(select(func.count()).select_from(m).where(m.organization_id == org))  # noqa: E731
    assert count(Document, demo.id) and count(ConstructionObject, demo.id) and count(KnowledgeChunk, demo.id)
    users = count(User, demo.id)

    assert client.post("/api/org/reset", headers=auth, json={"confirm": "да"}).status_code == 400
    r = client.post("/api/org/reset", headers=auth, json={"confirm": "УДАЛИТЬ"})
    assert r.status_code == 200 and r.json()["rows"] > 0
    db.expire_all()
    for m in (Document, ConstructionObject, KnowledgeChunk):
        assert count(m, demo.id) == 0
    assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert count(User, demo.id) == users and db.get(Organization, demo.id) is not None
    assert count(ConstructionObject, other.id) == 1  # чужая организация не затронута
    assert client.get("/api/objects", headers=auth).status_code == 200  # вход и работа — как раньше

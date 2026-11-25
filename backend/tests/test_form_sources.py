from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from bis.models import (
    Appointment,
    ConstructionObject,
    Counterparty,
    Document,
    KnowledgeChunk,
    KnowledgeSource,
    Organization,
    Person,
    User,
)
from bis.models.facts import FactSnapshot, ProjectFact
from bis.services import project_facts
from bis.services.form_sources import resolve


def _mapping(**fields):
    return SimpleNamespace(entries=[
        SimpleNamespace(target=str(i), field=name, source=source)
        for i, (name, source) in enumerate(fields.items())
    ])


def _scope(db, org_name, project_name):
    org = Organization(name=org_name, inn="7722446688", address="ул. Проектная, 17")
    db.add(org)
    db.flush()
    project = ConstructionObject(organization_id=org.id, name=project_name, address="ул. Участка, 4")
    db.add(project)
    db.flush()
    return org, project


def _fact(db, project, predicate, value, *, status="approved", actual=True, key=None, date=None):
    quote = f"{predicate}: {value}"
    doc = Document(organization_id=project.organization_id, object_id=project.id, kind="work_journal",
                   title=f"Журнал {value}", status=status, source="uploaded", file_key=key, date=date)
    db.add(doc)
    db.flush()
    snapshot = FactSnapshot(organization_id=project.organization_id, object_id=project.id, document_id=doc.id,
                            file_key=key, origin_key=f"document:{doc.id}", fingerprint="7" * 64,
                            text_sha256="8" * 64, title=doc.title, kind=doc.kind, state="active", method="rules")
    db.add(snapshot)
    db.flush()
    fact = ProjectFact(organization_id=project.organization_id, object_id=project.id, snapshot_id=snapshot.id,
                       predicate=predicate, category="actual" if actual else "planned",
                       value=value, normalized_value=value.casefold(), quote=quote, page=3)
    db.add(fact)
    db.flush()
    return doc, fact


def test_resolves_current_registry_and_scoped_document_evidence(db):
    org, project = _scope(db, "Наша организация", "Объект A")
    _, foreign = _scope(db, "Другая организация", "Объект B")
    sibling = ConstructionObject(organization_id=org.id, name="Участок вне выбранного проекта")
    db.add(sibling)
    db.flush()
    doc, fact = _fact(db, project, "work_start", "2025-02-03")
    _fact(db, project, "work", "Запланированное значение", actual=False)
    _fact(db, project, "work_start", "2025-02-12", status="replaced")
    _fact(db, project, "work_start", "2025-02-22", status="archived")
    stale, _ = _fact(db, project, "work_start", "2025-02-23", key="old/key")
    stale.file_key = "new/key"
    _fact(db, foreign, "work_start", "2025-03-04")
    _fact(db, sibling, "work_start", "2025-04-01")
    db.commit()
    user = SimpleNamespace(organization_id=org.id)

    fields = resolve(db, user, _mapping(org_name="auto", object_address="registry", work_start="documents",
                                        project_docs="auto", works="auto"), object_id=project.id)["fields"]
    assert fields["0"]["value"] == "Наша организация"
    assert fields["1"]["value"] == "ул. Участка, 4"
    assert fields["2"]["state"] == "resolved"
    assert fields["2"]["value"] == "03.02.2025"
    assert fields["2"]["sources"] == [{
        "id": f"fact:{fact.id}", "kind": "documents", "label": doc.title, "value": "03.02.2025",
        "document_id": str(doc.id), "page": 3, "quote": "work_start: 2025-02-03",
    }]
    assert fields["3"]["state"] == fields["4"]["state"] == "missing"
    with pytest.raises(LookupError):
        resolve(db, user, _mapping(object_name="auto"), object_id=foreign.id)


def test_excludes_facts_from_documents_dated_after_the_act(db):
    org, project = _scope(db, "Организация", "Объект")
    _fact(db, project, "material", "Кабель", date=dt.date(2025, 4, 1))
    _fact(db, project, "material", "Труба", date=dt.date(2025, 4, 3))
    db.commit()

    fields = resolve(db, SimpleNamespace(organization_id=org.id), _mapping(materials="documents"),
                     object_id=project.id, act_date=dt.date(2025, 4, 2))["fields"]
    assert fields["0"]["state"] == "resolved"
    assert fields["0"]["value"] == "Кабель"


def test_uses_current_indexed_text_before_typed_facts_exist(db):
    org, project = _scope(db, "Наша организация", "Объект")
    _, foreign = _scope(db, "Другая организация", "Другой объект")
    def add_source(obj, code, source_key, **meta):
        raw = Document(organization_id=obj.organization_id, object_id=obj.id, kind="certificate",
                       title="Протокол качества", status="registered", source="uploaded", file_key="version-2")
        db.add(raw)
        db.flush()
        source = KnowledgeSource(organization_id=obj.organization_id, document_id=raw.id, kind="org_document",
                                 title=raw.title, file_key=source_key, status="active", meta=meta)
        db.add(source)
        db.flush()
        db.add(KnowledgeChunk(source_id=source.id, organization_id=obj.organization_id,
                              ord=0, page=4, content=f"Сертификат № {code}"))
        return raw

    document = add_source(project, "CQ-2025", "version-2")
    add_source(project, "CQ-STALE", "version-1")
    add_source(project, "CQ-VISION", "version-2", vision=True)
    add_source(foreign, "CQ-FOREIGN", "version-2")
    db.commit()

    resolved = resolve(db, SimpleNamespace(organization_id=org.id), _mapping(certificate_number="documents"),
                       object_id=project.id)["fields"]["0"]
    assert resolved["state"] == "resolved"
    assert resolved["value"] == "CQ-2025"
    source, = resolved["sources"]
    assert (source["document_id"], source["page"], source["quote"]) == (str(document.id), 4, "Сертификат № CQ-2025")


def test_conflict_requires_current_candidate_or_manual_value(db):
    org, project = _scope(db, "Организация", "Объект")
    _, first = _fact(db, project, "work_end", "2025-04-01")
    _, second = _fact(db, project, "work_end", "2025-04-02")
    db.commit()
    user = SimpleNamespace(organization_id=org.id)
    mapping = _mapping(work_end="auto")
    unresolved = resolve(db, user, mapping, object_id=project.id)["fields"]["0"]
    assert unresolved["state"] == "conflict"
    assert unresolved["value"] is None
    assert {source["id"] for source in unresolved["sources"]} == {f"fact:{first.id}", f"fact:{second.id}"}
    selected = resolve(db, user, mapping, object_id=project.id, selections={"0": f"fact:{second.id}"})["fields"]["0"]
    assert selected["value"] == "02.04.2025"
    assert selected["sources"][0]["id"] == f"fact:{second.id}"
    with pytest.raises(ValueError, match="Источник"):
        resolve(db, user, mapping, object_id=project.id, selections={"0": "different-project"})
    manual = resolve(db, user, mapping, object_id=project.id, values={"0": "Проверено вручную"})["fields"]["0"]
    assert manual["sources"][0]["kind"] == "manual"


def test_honors_saved_decision_only_while_all_considered_facts_are_current(db):
    org, project = _scope(db, "Организация", "Объект")
    user = User(organization_id=org.id, email="pavel@example.test", full_name="Павел", password_hash="test")
    db.add(user)
    _, first = _fact(db, project, "work_end", "2025-04-01")
    _, second = _fact(db, project, "work_end", "2025-04-02")
    first.location_key = second.location_key = "этаж 1"
    first.work_key = second.work_key = "армирование"
    db.commit()
    ctx = SimpleNamespace(db=db, org_id=org.id, user=user, focus={})
    conflict, = project_facts.reconcile(ctx, project.id)
    project_facts.decide(ctx, conflict.id, second.id, conflict.revision, "Сверено с подписанным журналом")
    db.commit()

    mapping = _mapping(work_end="auto")
    chosen = resolve(db, user, mapping, object_id=project.id)["fields"]["0"]
    assert chosen["value"] == "02.04.2025"
    assert chosen["sources"][0]["id"] == f"fact:{second.id}"
    _, new = _fact(db, project, "work_end", "2025-04-03")
    new.location_key, new.work_key = first.location_key, first.work_key
    db.commit()
    reopened = resolve(db, user, mapping, object_id=project.id)["fields"]["0"]
    assert reopened["state"] == "conflict"
    assert len(reopened["sources"]) == 3


def test_signer_uses_role_validity_and_participant_organization(db):
    org, project = _scope(db, "Подрядчик", "Объект")
    developer = Counterparty(organization_id=org.id, name="Заказчик")
    db.add(developer)
    db.flush()
    project.developer_id = developer.id
    for name, cp, valid_from, valid_to in (
        ("Иванов Иван Иванович", developer, dt.date(2025, 1, 1), dt.date(2025, 5, 1)),
        ("Петров Пётр Петрович", developer, dt.date(2025, 5, 2), None),
        ("Чужой Работник Иной", None, dt.date(2025, 1, 1), None),
    ):
        person = Person(organization_id=org.id, full_name=name, position="Инженер контроля",
                        counterparty_id=cp.id if cp else None)
        db.add(person)
        db.flush()
        db.add(Appointment(organization_id=org.id, object_id=project.id, person_id=person.id,
                           role="developer_control", order_number="15", order_date=valid_from,
                           valid_from=valid_from, valid_to=valid_to))
    db.commit()
    user = SimpleNamespace(organization_id=org.id)

    fields = resolve(db, user, _mapping(developer_name="auto", signature_developer_control="registry"),
                     object_id=project.id, act_date=dt.date(2025, 3, 1))["fields"]
    assert fields["0"]["value"] == "Заказчик"
    assert fields["1"]["state"] == "resolved"
    assert "Иванов" in fields["1"]["value"]
    assert "15" in fields["1"]["sources"][0]["label"]

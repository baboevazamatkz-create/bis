from bis.services.id_work_index import work_index


def test_project_work_index_keeps_source_and_uses_subobject_when_present():
    result = work_index(
        [("rd", 5, "Участок 2; Подобъект К-1; Работы: Армирование плиты\nУчасток 3; Работы: Бетонирование")],
        [
            {"id": "act-1", "title": "АОСР № 17", "works": "Армирование плиты; участок 2; подобъект К-1"},
            {"id": "act-2", "title": "АОСР № 18", "works": "Бетонирование; участок 3; участок 4"},
            {"id": "act-3", "title": "АОСР № 19", "works": "Сварка"},
        ],
    )
    section, other = result["areas"]
    assert section["label"] == "Участок 2"
    work, = section["subobjects"][0]["works"]
    assert work["name"] == "Армирование плиты"
    assert work["act_ids"] == ["act-1"]
    assert work["source"] == {"document_id": "rd", "page": 5}
    assert other["works"][0]["act_ids"] == []
    assert result["unassigned_act_ids"] == ["act-2", "act-3"]


def test_declared_work_uses_only_unambiguous_context_in_the_same_chunk():
    result = work_index(
        [
            ("rd", 4, "КОРПУС Б\nРаботы: Монтаж трубопровода"),
            ("rd", 5, "Работы: Гидроизоляция"),
            ("rd", 6, "Участок 1\nУчасток 2\nРаботы: Освещение"),
        ],
        [],
    )
    by_label = {area["label"]: area["works"] for area in result["areas"]}
    assert by_label["Корпус Б"][0]["name"] == "Монтаж трубопровода"
    assert by_label["Участок 1"] == by_label["Участок 2"] == []


def test_named_places_and_planned_works_are_grounded_in_the_project():
    result = work_index(
        [("rd", 3, "Участок: Северный; Подобъект: Насосная станция; предусмотрено устройство водоотвода.")],
        [{"id": "ambiguous", "title": "АОСР", "works": "Монтаж на участке 1 и 2"}],
    )
    area, = result["areas"]
    assert area["label"] == "Участок Северный"
    assert area["subobjects"][0]["label"] == "Подобъект Насосная станция"
    assert area["subobjects"][0]["works"][0]["name"] == "устройство водоотвода"
    assert result["unassigned_act_ids"] == ["ambiguous"]


def test_workspace_index_only_uses_the_selected_project(client, auth, db, demo):
    from bis.models import ConstructionObject, Document, KnowledgeChunk, KnowledgeSource

    def add_document(project, kind, title, text=None, works=None):
        doc = Document(organization_id=demo.id, object_id=project.id, kind=kind, title=title,
                       data={"works": works} if works else {})
        db.add(doc)
        db.flush()
        if text:
            source = KnowledgeSource(organization_id=demo.id, document_id=doc.id, kind=kind, title=title)
            db.add(source)
            db.flush()
            db.add(KnowledgeChunk(organization_id=demo.id, source_id=source.id, ord=0, page=3, content=text))
        return str(doc.id)

    project = ConstructionObject(organization_id=demo.id, name="Проект ИД")
    other = ConstructionObject(organization_id=demo.id, name="Другой объект")
    db.add_all([project, other])
    db.flush()
    rd_id = add_document(project, "project_doc", "Чертёж", "Участок 42; Работы: Монтаж")
    act_id = add_document(project, "aosr", "Акт № 42", works="Монтаж на участке 42")
    orphan_id = add_document(project, "aook", "Без привязки")
    add_document(other, "project_doc", "Другой чертёж", "Участок 99; Работы: Окраска")
    add_document(other, "aosr", "Другой акт", works="Окраска на участке 99")
    db.commit()

    empty = client.get("/api/workspace/id-structure", headers=auth)
    assert empty.status_code == 200 and empty.json()["needs_project"]
    response = client.get("/api/workspace/id-structure", headers={**auth, "X-BIS-Project": str(project.id)})
    assert response.status_code == 200, response.text
    body = response.json()
    area, = body["areas"]
    assert area["label"] == "Участок 42"
    assert area["works"] == [{"name": "Монтаж", "source": {"document_id": rd_id, "page": 3}, "act_ids": [act_id]}]
    assert body["unassigned_act_ids"] == [orphan_id]
    assert body["indexed_documents"] == 1

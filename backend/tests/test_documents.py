import io
from datetime import date

import docx

from bis.documents import AOSRData, LetterData, render_aosr, render_letter, validate_aosr
from bis.documents.aosr import Representative
from bis.documents.common import Party, docx_text, fmt_date_long, short_name
from bis.documents.templates import render_template, template_context

DEV = Party(name="АО «Заказчик»", ogrn="1027700000003", inn="7702000003", address="г. Москва")
BUILDER = Party(name="ООО «СтройДемо»", ogrn="1027700000001", inn="7701000001", sro="СРО «Демо»")


def aosr(**kw) -> AOSRData:
    base = dict(
        number="12", date=date(2026, 5, 20), object_name="Жилой дом, корпус 1, г. Москва", developer=DEV,
        builder=BUILDER,
        representatives={
            "developer_control": Representative(position="Инженер СК", full_name="Кузнецов Дмитрий Андреевич",
                                                nrs_id="С-77-000001", order="приказ № 15-СК от 20.02.2026"),
            "builder": Representative(position="Начальник участка", full_name="Орлов Павел Игоревич",
                                      order="приказ № 21-к от 20.02.2026"),
            "builder_control": Representative(position="ГИП", full_name="Волков Сергей Николаевич",
                                              nrs_id="С-77-000002", order="приказ № 22-к от 20.02.2026"),
        },
        works="Армирование фундаментной плиты в осях 1-5/А-Г",
        project_docs="ЖК-01-КЖ1, листы 3-7, ООО «ПроектДемо»",
        materials=["Арматура А500С d12, сертификат качества № 123 от 01.05.2026"],
        executive_docs=["Исполнительная схема № 5"],
        work_start=date(2026, 5, 10), work_end=date(2026, 5, 18),
        norms=["ДЕМО-СП 999.13330.2026, п. 4.2.1-4.2.3"], next_works="Бетонирование фундаментной плиты",
    )
    base.update(kw)
    return AOSRData(**base)


def test_aosr_valid_and_rendered():
    d = aosr()
    assert [i for i in validate_aosr(d) if i.level == "error"] == []
    text = docx_text(render_aosr(d))
    assert "освидетельствования скрытых работ" in text
    assert "Армирование фундаментной плиты" in text
    assert "С-77-000001" in text
    assert "Кузнецов Д.А." in text
    assert "«20» мая 2026 г." in text


def test_aosr_validation_errors():
    d = aosr(work_end=date(2026, 5, 1), materials=["Бетон B25"], date=date(2026, 4, 1))
    d.representatives["builder_control"].nrs_id = None
    del d.representatives["developer_control"]
    issues = {(i.level, i.field) for i in validate_aosr(d)}
    assert ("error", "work_end") in issues
    assert ("error", "representatives.developer_control") in issues
    assert ("error", "representatives.builder_control.nrs_id") in issues
    assert ("warning", "materials.0") in issues


def test_letter_render():
    d = LetterData(sender=BUILDER, number="15/26", date=date(2026, 6, 1), addressee=["Генеральному директору",
                   "АО «Заказчик»", "А.В. Смирнову"], subject="О направлении исполнительной документации",
                   greeting="Уважаемый Алексей Викторович!", body=["Направляем комплект ИД.", "Просим подписать."],
                   attachments=["АОСР № 12 на 2 л. в 1 экз."], signer_position="Генеральный директор",
                   signer_name="И.И. Иванов", executor="Петрова А.С.")
    text = docx_text(render_letter(d))
    for s in ("ООО «СтройДемо»", "01.06.2026 № 15/26", "А.В. Смирнову", "О направлении", "Приложение: АОСР № 12",
              "И.И. Иванов", "Исп. Петрова А.С."):
        assert s in text, s


def test_org_template():
    tpl = docx.Document()
    tpl.add_paragraph("АКТ № {{ number }} от {{ date_text }}")
    tpl.add_paragraph("Застройщик: {{ developer_text }}")
    tpl.add_paragraph("{% for m in materials %}{{ m }}; {% endfor %}")
    buf = io.BytesIO()
    tpl.save(buf)
    out = docx_text(render_template(buf.getvalue(), template_context(aosr())))
    assert "АКТ № 12 от 20.05.2026" in out
    assert "Застройщик: АО «Заказчик», ОГРН 1027700000003" in out
    assert "Арматура А500С" in out


def test_helpers():
    assert short_name("Петров Пётр Петрович") == "Петров П.П."
    assert short_name("Петров П.П.") == "Петров П.П."
    assert fmt_date_long(date(2026, 1, 5)) == "«05» января 2026 г."


# --- АООК, реестр ИД, журнал работ ---------------------------------------------------------------


def _aook_body(object_id: str, **kw) -> dict:
    return {"object_id": object_id, "works": "Фундаментная плита Пм1 в осях 1-9/А-В",
            "project_docs": "ЖК-01-КЖ0, листы 1-12, ООО «ПроектДемо»", "materials": ["Бетон B25 W8 F150 — паспорт № 77"],
            "executive_docs": ["Исполнительная схема плиты Пм1"], "work_start": "2026-05-18", "work_end": "2026-06-30",
            "norms": ["СП 70.13330.2012, п. 5.17"], **kw}


def test_aook_validation_and_render(client, auth):
    import io

    from docx import Document as Docx

    obj = client.get("/api/objects", headers=auth).json()[0]
    check = client.post("/api/documents/aook/check", headers=auth,
                        json=_aook_body(obj["id"], permit="partial")).json()
    fields = {i["field"] for i in check["issues"]}
    assert {"aosr_refs", "tests", "load_percent"} <= fields
    r = client.post("/api/documents/aook", headers=auth,
                    json=_aook_body(obj["id"], aosr_refs=["АОСР № 12 от 10.06.2026"], tests=["Протокол № 45"]))
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["kind"] == "aook"
    text = "\n".join(p.text for p in Docx(io.BytesIO(client.get(f"/api/documents/{doc['id']}/file", headers=auth).content)).paragraphs)
    assert "ответственных конструкций" in text.lower() and "Фундаментная плита Пм1" in text


def test_id_registry_and_work_journal(client, auth):
    obj = client.get("/api/objects", headers=auth).json()[0]
    reg = client.get(f"/api/objects/{obj['id']}/id-registry", headers=auth).json()
    # в демо есть объёмы скрытых работ без АОСР — реестр показывает это как ошибку
    assert reg["hidden_volumes"] > 0 and reg["coverage_percent"] == 0
    assert any(i["level"] == "error" and "без утверждённого АОСР" in i["message"] for i in reg["issues"])
    exp = client.post(f"/api/objects/{obj['id']}/id-registry/export", headers=auth)
    assert exp.status_code == 201 and exp.json()["kind"] == "id_registry"
    j = client.post(f"/api/objects/{obj['id']}/work-journal", headers=auth,
                    json={"period_start": "2026-05-01", "period_end": "2026-05-31"})
    assert j.status_code == 201, j.text
    assert j.json()["kind"] == "work_journal"

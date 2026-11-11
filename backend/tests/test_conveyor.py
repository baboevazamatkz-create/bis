"""ИИ-конвейер: сигналы с производства → специалисты → действия, решения людей, обучение на правках."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from bis.ai import conveyor as cv
from bis.ai import understanding as und
from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.models import (
    AIAction,
    Contract,
    Document,
    EstimateItem,
    Material,
    MaterialReceipt,
    MaterialUsage,
    OrgMemory,
    Task,
    User,
    WorkVolume,
)
from bis.rag.embeddings import get_embedder
from bis.services.storage import get_storage
from tests.conftest import login

UPD = """Универсальный передаточный документ
Счет-фактура № 214 от 20.05.2026
Продавец: ООО «БетонСтрой» ИНН 7705123456
Покупатель: ООО «СтройДемо»
1 | Бетон B25 W8 F150 | м3 | 120 | 8 500,00 | 1 020 000,00
2 | Арм. стержни 12 мм | т | 5,5 | 72 000,00 | 396 000,00
Всего к оплате 1 416 000,00"""

CERT = """ДОКУМЕНТ О КАЧЕСТВЕ БЕТОННОЙ СМЕСИ № 77 от 20.05.2026
Изготовитель: ООО «БетонСтрой»
Бетонная смесь: Бетон B25 W8 F150 П4
Партия № 0520-3
Действителен до 20.11.2026"""

PROTOCOL = """ПРОТОКОЛ ИСПЫТАНИЙ № 45 от 18.06.2026
Определение прочности бетона на сжатие
Конструкция: фундаментная плита Пм1, захватка 2
Требуемая прочность: 32,7 МПа (B25)
Фактическая прочность: 27,4 МПа
Заключение: не соответствует требованиям"""

LETTER = """АО «Заказчик-Девелопмент»
ИНН 7702000003
Исх. № 145/ТН от 02.11.2026
О предоставлении исполнительной документации
В соответствии с договором генподряда № ГП-01/2026 просим в течение 5 рабочих дней предоставить исполнительную документацию на армирование фундаментной плиты.
Технический директор Петров П.П."""

SURVEY = "Точка;Xпр;Yпр;Xф;Yф;Hпр;Hф\n1;0;0;0,008;-0,005;-3,200;-3,195\n2;24;0;24,032;0,004;-3,200;-3,214\n" \
         "3;24;18;23,990;18,021;-3,200;-3,203\n"

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def make_env(db, demo, *, gateway=None, email="admin@demo.bis") -> cv.Env:
    user = db.scalar(select(User).where(User.email == email))
    return cv.Env(db=db, storage=get_storage(), gateway=gateway, embedder=get_embedder(), org_id=demo.id, user=user)


def run(env: cv.Env, *, text=None, data=None, name=None, mime=None):
    event = cv.ingest(env, source="upload", text=text, data=data, file_name=name, mime=mime)
    actions = cv.process(env, event)
    assert event.status == "done", event.error
    return event, actions


def by_kind(actions, kind):
    return [a for a in actions if a.kind == kind]


def item(db, demo, code) -> EstimateItem:
    return db.scalar(select(EstimateItem).where(EstimateItem.organization_id == demo.id, EstimateItem.code == code))


def material(db, demo, name) -> Material:
    return db.scalar(select(Material).where(Material.organization_id == demo.id, Material.name == name))


def test_detect_kind_and_parsing():
    assert cv.detect_kind("IMG_01.jpg", None) == "photo"
    assert cv.detect_kind("voice.ogg", None) == "voice"
    assert cv.detect_kind("upd.pdf", "application/pdf") == "document"
    r = und.heuristic_report("Закончили армирование плиты Пм1 захватка 3, 12 т. Израсходовали бетон В25 310 м3")
    assert (r.works[0].quantity, r.works[0].unit, r.works[0].location) == (12, "т", "захватка 3")
    assert (r.materials[0].name, r.materials[0].quantity) == ("бетон В25", 310)  # «В25» — класс, а не часть числа


def test_supply_document_proposes_receipts_and_learns(db, demo):
    env = make_env(db, demo)
    event, actions = run(env, data=UPD.encode(), name="УПД_214.txt", mime="text/plain")
    assert event.detected_type == "upd" and event.object_id is not None  # единственный активный объект
    reg = by_kind(actions, "register_document")[0]
    assert reg.status == "applied" and db.get(Document, reg.result["document_id"]).kind == "supply_doc"
    receipts = by_kind(actions, "create_receipt")
    assert [a.status for a in receipts] == ["proposed", "proposed"]  # приход — только с подтверждения человека
    concrete, rebar = receipts
    assert concrete.payload["material_id"] == str(material(db, demo, "Бетон B25 W8 F150").id)
    assert rebar.payload["material_id"] is None and rebar.payload["new_material"]["name"] == "Арм. стержни 12 мм"
    task = by_kind(actions, "create_task")[0]  # в накладной нет сертификатов → поручение снабжению
    assert task.status == "applied" and db.get(Task, task.result["task_id"]).due_date == dt.date.today() + dt.timedelta(days=2)

    # человек утверждает бетон → приход создан, контролёр видит цену выше обычной (8500 против 7200)
    admin = env.user
    cv.review(env, concrete, approve=True, user=admin)
    rec = db.get(MaterialReceipt, concrete.result["receipt_id"])
    assert rec.quantity == D(120) and rec.price == D(8500) and rec.document_ref == "УПД № 214 от 20.05.2026"
    flags = [a for a in env.created if a.kind == "flag_issue"]
    assert flags and "Цена выше обычной" in flags[0].title and flags[0].status == "applied"

    # арматуру ИИ не узнал — человек исправляет материал; ИИ запоминает название
    a500 = material(db, demo, "Арматура А500С")
    cv.review(env, rebar, approve=True, user=admin, changes={"material_id": str(a500.id)})
    assert rebar.status == "applied" and rebar.corrected
    assert db.get(MaterialReceipt, rebar.result["receipt_id"]).material_id == a500.id
    assert db.scalar(select(func.count()).select_from(Material).where(Material.name == "Арм. стержни 12 мм")) == 0
    found, score = cv.match_material(env, "Арм. стержни 12 мм", "т")
    assert found.id == a500.id and score == 1.0
    with pytest.raises(ValueError):
        cv.review(env, rebar, approve=False, user=admin)  # уже рассмотрено


def test_reject_rolls_back_and_becomes_rule(db, demo):
    env = make_env(db, demo)
    _, actions = run(env, text="Залили бетон плиты Пм1 захватка 3 — 310 м3.")
    vol = by_kind(actions, "report_volume")[0]
    assert vol.status == "applied" and vol.auto  # объём ИИ вносит сам
    assert vol.payload["estimate_item_id"] == str(item(db, demo, "1.3").id)
    wv_id = vol.result["volume_id"]
    cv.review(env, vol, approve=False, user=env.user, reason="Это была захватка 4, объём уточнит мастер")
    assert vol.status == "rejected" and db.get(WorkVolume, wv_id) is None
    memo = db.scalars(select(OrgMemory).where(OrgMemory.kind == "feedback")).all()
    assert {m.scope for m in memo} >= {"dispatcher"} and "захватка 4" in memo[0].content
    assert "захватка 4" in cv.hints(env, "dispatcher")


def test_foreman_report_completion_assembles_aosr(db, demo):
    env = make_env(db, demo)
    _, actions = run(env, text="Закончили армирование фундаментной плиты Пм1 захватка 3, 12 т, готово к освидетельствованию. "
                               "Израсходовано арматуры 12 т.")
    vol = by_kind(actions, "report_volume")[0]
    assert vol.status == "applied" and vol.payload["location"] == "захватка 3" and vol.payload["complete"]
    usage = by_kind(actions, "record_usage")[0]
    assert usage.status == "applied" and db.get(MaterialUsage, usage.result["usage_id"]).quantity == D(12)
    aosr = by_kind(actions, "draft_aosr")[0]  # работа скрытая и закончена → инженер ПТО собирает АОСР сам
    assert aosr.specialist == "pto" and aosr.status == "applied", aosr.result
    doc = db.get(Document, aosr.result["document_id"])
    assert doc.kind == "aosr" and doc.status == "draft"
    assert "Армирование фундаментной плиты Пм1" in doc.data["works"] and "захватка 3" in doc.data["works"]
    assert any("Арматура А500С" in m for m in doc.data["materials"])
    linked = db.scalars(select(WorkVolume).where(WorkVolume.aosr_document_id == doc.id)).all()
    assert [v.location for v in linked] == ["захватка 3"]  # акт на сданную захватку, а не на всё накопленное
    assert "исполнительная схема" in aosr.summary  # честно говорит, чего не хватает

    # по той же захватке черновик уже ждёт подписи — второй не нужен; новая захватка — свой акт
    _, again = run(env, text="Закончили армирование фундаментной плиты Пм1 захватка 3, 1 т, готово к освидетельствованию")
    assert not by_kind(again, "draft_aosr")
    _, other = run(env, text="Закончили армирование фундаментной плиты Пм1 захватка 4, 3 т, готово к освидетельствованию")
    assert "захватка 4" in by_kind(other, "draft_aosr")[0].title

    # отклонение черновика откатывает привязку объёмов
    cv.review(env, aosr, approve=False, user=env.user)
    assert db.get(Document, doc.id).status == "rejected"
    assert not db.scalars(select(WorkVolume).where(WorkVolume.aosr_document_id == doc.id)).all()


def test_learning_work_alias(db, demo):
    env = make_env(db, demo)
    _, actions = run(env, text="вязали каркас захватка 4, 5 т")
    rebar_item = item(db, demo, "1.2")
    first = actions[0]
    if first.kind == "report_volume":  # ИИ ошибся с позицией или не уверен — человек поправляет
        cv.review(env, first, approve=True, user=env.user, changes={"estimate_item_id": str(rebar_item.id)})
    else:
        cv.remember_alias(env, "works", "вязали каркас захватка 4, 5 т", rebar_item.id)
    found, score = cv.match_work(env, "вязали каркас захватка 5, 6 т", "т", rebar_item.object_id)
    assert found.id == rebar_item.id and score >= 0.5


def test_certificate_links_to_receipts(db, demo):
    env = make_env(db, demo)
    b25 = material(db, demo, "Бетон B25 W8 F150")
    rec = MaterialReceipt(organization_id=demo.id, object_id=db.scalar(select(EstimateItem.object_id).limit(1)),
                          material_id=b25.id, date=dt.date(2026, 5, 20), quantity=D(120), document_ref="УПД № 214")
    db.add(rec)
    db.flush()
    event, actions = run(env, data=CERT.encode(), name="Паспорт_77.txt")
    assert event.detected_type == "certificate"
    link = by_kind(actions, "link_certificate")[0]
    assert link.status == "applied" and str(rec.id) in link.result["receipt_ids"]
    assert "№ 77" in rec.quality_doc and "партия 0520-3" in rec.quality_doc
    cv.review(env, link, approve=False, user=env.user)  # откат — документ снова не привязан
    assert rec.quality_doc is None


def test_noncompliant_protocol_raises_critical_issue(db, demo):
    env = make_env(db, demo)
    _, actions = run(env, data=PROTOCOL.encode(), name="Протокол_45.txt")
    flag = [a for a in actions if a.kind == "flag_issue"][0]
    assert flag.severity == "critical" and flag.specialist == "verifier" and flag.result["notified"] >= 1
    task = by_kind(actions, "create_task")[0]
    assert db.get(Task, task.result["task_id"]).priority == "high"
    assert by_kind(actions, "register_document")[0].status == "applied"


def test_photo_without_and_with_vision(db, demo):
    env = make_env(db, demo)
    event, actions = run(env, data=PNG, name="IMG_0001.png", mime="image/png", text="захватка 3")
    assert event.kind == "photo" and event.detected_type == "site_photo"
    assert any("Модель зрения не подключена" in (a.summary or "") for a in actions)
    assert db.get(Document, event.document_id).kind == "photo"

    vision = ScriptedProvider([json.dumps({
        "is_document": False, "work_type": "армирование плиты", "description": "Каркас плиты, захватка 3",
        "completion_percent": 80, "defects": [{"description": "Нет фиксаторов защитного слоя", "severity": "medium"}],
        "safety_violations": ["Рабочий без каски"]}, ensure_ascii=False)], name="vlm")
    gw = LLMGateway(entries={"vlm": ProviderEntry(vision, capabilities=("text", "vision"))}, routes={"default": ["vlm"]})
    set_gateway(gw)
    env2 = make_env(db, demo, gateway=gw)
    event2, actions2 = run(env2, data=PNG, name="IMG_0002.png", mime="image/png")
    assert vision.calls and vision.calls[0]["messages"][1].images  # фото ушло модели зрения
    titles = [a.title for a in actions2]
    assert any("готовность ~80%" in t for t in titles)
    assert any(t.startswith("Дефект") for t in titles) and any("охраны труда" in t for t in titles)
    assert by_kind(actions2, "create_task")[0].status == "applied"


def test_voice_without_stt_is_flagged(db, demo):
    env = make_env(db, demo)
    event, actions = run(env, data=b"OggS fake", name="voice.ogg", mime="audio/ogg")
    assert event.kind == "voice" and actions[0].specialist == "hearing" and actions[0].severity == "warning"


def test_letter_is_registered_and_processed(db, demo):
    env = make_env(db, demo)
    event, actions = run(env, data=LETTER.encode(), name="Письмо_145.txt")
    assert event.detected_type == "letter"
    proc = by_kind(actions, "process_letter")[0]
    assert proc.status == "applied", proc.result
    doc = db.get(Document, proc.result["document_id"])
    assert doc.kind == "incoming_letter" and doc.number.startswith("вх-")


def test_contract_import_after_approval(db, demo):
    from pathlib import Path

    import bis.demo as demo_pkg

    env = make_env(db, demo)
    text = (Path(demo_pkg.__file__).parent / "demo_contract.txt").read_bytes()
    event, actions = run(env, data=text, name="Договор_новый.txt")
    assert event.detected_type == "contract"
    imp = by_kind(actions, "import_contract")[0]
    assert imp.status == "proposed"  # договор — только с подтверждения
    before = db.scalar(select(func.count()).select_from(Contract))
    cv.review(env, imp, approve=True, user=env.user)
    assert imp.status == "applied", imp.result
    assert db.scalar(select(func.count()).select_from(Contract)) == before + 1 and imp.result["obligations_added"] > 0


def test_survey_builds_scheme(db, demo):
    env = make_env(db, demo)
    event, actions = run(env, data=SURVEY.encode(), name="съёмка_плита.csv", mime="text/csv")
    assert event.detected_type == "survey"
    sch = by_kind(actions, "build_scheme")[0]
    assert sch.status == "applied" and db.get(Document, sch.result["document_id"]).kind == "scheme"
    assert any("Превышение допусков" in a.title for a in actions)  # точка 2: +32 мм при допуске 15


def test_autonomy_policy_and_suggestions(db, demo):
    org = demo
    org.settings = {**org.settings, "autonomy": {"report_volume": "propose"}}
    db.flush()
    env = make_env(db, demo)
    _, actions = run(env, text="Залили бетон плиты Пм1 захватка 3 — 310 м3.")
    vol = by_kind(actions, "report_volume")[0]
    assert vol.status == "proposed"  # организация решила: объёмы — только с подтверждения
    now = dt.datetime.now(dt.UTC)
    for _ in range(10):
        db.add(AIAction(organization_id=org.id, specialist="dispatcher", kind="report_volume", title="x", payload={},
                        evidence=[], result={}, status="applied", reviewed_at=now))
    db.flush()
    sugg = cv.autonomy_suggestions(db, org.id, env.autonomy())
    assert sugg and sugg[0]["kind"] == "report_volume" and sugg[0]["suggest"] == "auto"


def test_api_intake_feed_review(client, auth, db, demo):
    r = client.post("/api/intake", headers=auth, data={"text": "Залили бетон плиты Пм1 захватка 3 — 310 м3. "
                                                               "Не привезли мембрану, бригада простаивает."})
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["dispatcher"]["type"] == "foreman_report" and ev["object_name"]
    kinds = {a["kind"]: a for a in ev["actions"]}
    assert kinds["report_volume"]["status"] == "applied" and kinds["create_task"]["status"] == "applied"
    assert "Понял" in ev["reply"]

    feed = client.get("/api/ai/feed", headers=auth).json()
    assert feed["events"][0]["id"] == ev["id"]
    r = client.post(f"/api/ai/actions/{kinds['report_volume']['id']}/review", headers=auth,
                    json={"approve": True, "changes": {"quantity": 305}})
    assert r.status_code == 200, r.text
    assert r.json()["action"]["corrected"] and r.json()["action"]["payload"]["quantity"] == 305

    up = client.post("/api/intake", headers=auth, files={"file": ("УПД_214.txt", UPD.encode(), "text/plain")}).json()
    pending = [a["id"] for a in up["actions"] if a["status"] == "proposed"]
    assert len(pending) == 2
    assert client.get("/api/ai/feed?only=pending", headers=auth).json()["pending"] == 2
    batch = client.post("/api/ai/actions/review-batch", headers=auth, json={"ids": pending, "approve": True}).json()
    assert batch["done"] == 2 and not batch["errors"]
    f = client.get(f"/api/intake/{up['id']}/file", headers=auth)
    assert f.status_code == 200 and "БетонСтрой" in f.content.decode()

    sp = client.get("/api/ai/specialists", headers=auth).json()
    names = {s["name"]: s for s in sp["specialists"]}
    assert names["vision"]["connected"] is False and names["reader"]["stats"]["approved"] == 2
    assert client.put("/api/ai/autonomy", headers=auth, json={"modes": {"create_receipt": "auto"}}).status_code == 200
    pto = login(client, "pto@demo.bis")
    assert client.put("/api/ai/autonomy", headers=pto, json={"modes": {"create_receipt": "auto"}}).status_code == 403
    assert client.post("/api/intake", headers=auth, data={}).status_code == 400


def test_usage_matched_by_context_and_checked_against_norm(db, demo):
    env = make_env(db, demo)
    _, actions = run(env, text="Залили бетон плиты Пм1 захватка 4 — 290 м3. Бетона ушло 310 м3.")
    usage = by_kind(actions, "record_usage")[0]
    # «бетона» подходит и к B7,5, и к B25 — выбран бетон по норме работы из того же сообщения
    assert usage.payload["material_id"] == str(material(db, demo, "Бетон B25 W8 F150").id)
    assert "По норме 294.35 м3" in usage.summary and "перерасход" in usage.summary  # 290 × 1 + 1,5% потерь
    over = [a for a in actions if a.kind == "flag_issue" and "Перерасход к норме" in a.title]
    assert over and "+5.3%" in over[0].title

    _, ok = run(env, text="Залили бетон плиты Пм1 захватка 5 — 100 м3. Бетона ушло 101 м3.")
    assert "в пределах" in by_kind(ok, "record_usage")[0].summary
    assert not [a for a in ok if "Перерасход к норме" in a.title]


def test_scan_photo_and_voice_via_yandex_services(db, demo, monkeypatch):
    """Фото накладной → OCR → чтец документов; голосовое → SpeechKit → объёмы (транспорт подменён)."""
    from bis import ocr, voice
    from bis.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ocr_provider", "yandex")
    monkeypatch.setattr(s, "stt_provider", "yandex")
    monkeypatch.setattr(s, "yandex_folder_id", "b1gtest")
    monkeypatch.setenv(s.yandex_api_key_env, "fixture-ocr-key")
    monkeypatch.setattr(ocr, "recognize", lambda data, mime, client=None: UPD)
    monkeypatch.setattr(voice, "_transcribe_yandex", lambda audio, client: "залили бетон плиты Пм1 захватка 6 — 120 кубов")
    monkeypatch.setattr(voice.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    env = make_env(db, demo)
    event, actions = run(env, data=PNG, name="IMG_upd.jpg", mime="image/jpeg")
    assert event.detected_type == "upd" and event.result["ocr"] and event.result["photo_of_document"]
    assert len(by_kind(actions, "create_receipt")) == 2
    ev2, acts2 = run(env, data=b"OggS voice", name="voice.ogg", mime="audio/ogg")
    assert ev2.result["transcript"].endswith("120 м3")
    assert by_kind(acts2, "report_volume")[0].payload["quantity"] == 120

"""Демонстрационные данные: организация, объект, участники, приказы, договор, демо-норматив."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.models import (
    Appointment,
    ConstructionObject,
    Contract,
    Counterparty,
    KnowledgeSource,
    Organization,
    OrgMemory,
    Person,
    User,
)
from bis.rag.embeddings import Embedder
from bis.rag.ingest import ingest_bytes
from bis.security import hash_password
from bis.services.storage import Storage

DEMO_EMAIL = "admin@demo.bis"
DEMO_PASSWORD = "demo12345"


def create_organization(db: Session, *, name: str, admin_email: str, admin_password: str, admin_name: str,
                        inn: str | None = None, platform_admin: bool = False) -> tuple[Organization, User]:
    org = Organization(name=name, inn=inn, settings={})
    db.add(org)
    db.flush()
    admin = User(organization_id=org.id, email=admin_email.lower(), full_name=admin_name, role="admin",
                 password_hash=hash_password(admin_password), is_platform_admin=platform_admin)
    db.add(admin)
    db.flush()
    return org, admin


def seed_demo(db: Session, storage: Storage, embedder: Embedder) -> Organization:
    existing = db.scalar(select(User).where(User.email == DEMO_EMAIL))
    if existing:
        return db.get(Organization, existing.organization_id)
    org, admin = create_organization(db, name="ООО «СтройДемо»", admin_email=DEMO_EMAIL, admin_password=DEMO_PASSWORD,
                                     admin_name="Иванов Иван Иванович", inn="7701000001", platform_admin=True)
    org.kpp, org.ogrn = "770101001", "1027700000001"
    org.address = "123000, г. Москва, ул. Строителей, д. 1"
    org.phone, org.email = "+7 (495) 000-00-00", "info@stroydemo.example"
    org.sro = "Ассоциация «СРО Демо-Строй», ОГРН 1097700000002, ИНН 7701000002"
    org.settings = {"default_signer": {"position": "Генеральный директор", "name": "И.И. Иванов"},
                    "letter_number_format": "{seq}/{yy}"}
    admin.position = "Генеральный директор"
    for email, name, position, role in [
        ("pto@demo.bis", "Петрова Анна Сергеевна", "Инженер ПТО", "pto"),
        ("manager@demo.bis", "Сидоров Олег Петрович", "Главный инженер", "manager"),
    ]:
        db.add(User(organization_id=org.id, email=email, full_name=name, position=position, role=role,
                    password_hash=hash_password(DEMO_PASSWORD)))

    developer = Counterparty(organization_id=org.id, kind="developer", name="АО «Заказчик-Девелопмент»",
                             inn="7702000003", ogrn="1027700000003", address="125000, г. Москва, пр. Мира, д. 10",
                             phone="+7 (495) 111-11-11", head_position="Генеральному директору",
                             head_name="Смирнову А.В.", email="office@developer.example")
    designer = Counterparty(organization_id=org.id, kind="designer", name="ООО «ПроектДемо»", inn="7703000004",
                            ogrn="1027700000004", address="115000, г. Москва, ул. Проектная, д. 5",
                            sro="Ассоциация «СРО Проектировщиков», ОГРН 1097700000005, ИНН 7701000005")
    sub = Counterparty(organization_id=org.id, kind="subcontractor", name="ООО «МонолитСервис»", inn="7704000006",
                       ogrn="1027700000006", address="140000, Московская обл., г. Люберцы, ул. Заводская, д. 3")
    db.add_all([developer, designer, sub])
    db.flush()

    obj = ConstructionObject(organization_id=org.id, code="ЖК-01",
                             name="Многоквартирный жилой дом со встроенными помещениями, корпус 1",
                             address="г. Москва, ул. Новая, з/у 7", developer_id=developer.id,
                             designer_id=designer.id, start_date=date(2026, 3, 1))
    db.add(obj)
    db.flush()

    people = [
        ("Кузнецов Дмитрий Андреевич", "Инженер строительного контроля", developer.id, "С-77-000001",
         "developer_control", "15-СК"),
        ("Орлов Павел Игоревич", "Начальник участка", None, None, "builder", "21-к"),
        ("Волков Сергей Николаевич", "Главный инженер проекта", None, "С-77-000002", "builder_control", "22-к"),
        ("Белова Мария Олеговна", "Главный инженер проекта", designer.id, None, "designer", "7-АН"),
    ]
    responsible_id = None
    for full_name, position, cp_id, nrs, role, order in people:
        person = Person(organization_id=org.id, full_name=full_name, position=position, counterparty_id=cp_id,
                        nrs_id=nrs)
        db.add(person)
        db.flush()
        if role == "builder_control":
            responsible_id = person.id
        db.add(Appointment(organization_id=org.id, person_id=person.id, object_id=obj.id, role=role,
                           order_number=order, order_date=date(2026, 2, 20), valid_from=date(2026, 3, 1)))

    general = Contract(organization_id=org.id, kind="general", number="ГП-01/2026", date=date(2026, 2, 15),
                       subject="Строительство жилого дома, корпус 1", object_id=obj.id, counterparty_id=developer.id,
                       amount=850_000_000, vat_rate=20, start_date=date(2026, 3, 1), end_date=date(2027, 12, 31),
                       responsible_id=responsible_id)
    db.add(general)
    db.add(Contract(organization_id=org.id, kind="subcontract", number="СП-07/2026", date=date(2026, 3, 10),
                    subject="Монолитные работы, корпус 1", object_id=obj.id, counterparty_id=sub.id,
                    amount=120_000_000, vat_rate=20, start_date=date(2026, 3, 15), end_date=date(2026, 12, 31)))
    db.add(OrgMemory(organization_id=org.id, kind="style", scope="secretary", object_id=obj.id,
                     content="В письмах заказчику указывать номер договора генподряда ГП-01/2026, если письмо по корпусу 1.",
                     source="demo"))

    db.flush()
    from bis.services.contracts import analyze_contract, attach_contract_file

    contract_text = (Path(__file__).parent / "demo_contract.txt").read_bytes()
    doc = attach_contract_file(db, storage, general, contract_text, "Договор_ГП-01-2026.txt", "text/plain", "system:demo")
    ingest_bytes(db, storage, embedder, org_id=org.id, data=contract_text, filename="Договор_ГП-01-2026.txt",
                 kind="contract", title="Договор генподряда № ГП-01/2026", document_id=doc.id)
    analyze_contract(db, storage, None, general, actor="system:demo")

    _seed_works(db, org.id, obj.id, general.id, responsible_id)

    has_demo_norm = db.scalar(select(KnowledgeSource).where(KnowledgeSource.designation == "ДЕМО-СП 999.13330.2026"))
    if not has_demo_norm:
        data = (Path(__file__).parent / "demo_sp.txt").read_bytes()
        ingest_bytes(db, storage, embedder, org_id=None, data=data, filename="demo_sp.txt", kind="norm",
                     title="Бетонные и земляные работы (демонстрационный свод правил)",
                     designation="ДЕМО-СП 999.13330.2026", status="active")
    db.flush()
    return org


def _seed_works(db: Session, org_id, object_id, contract_id, responsible_id) -> None:
    """Смета, ГПР, материалы, нормы, приход, расход и объёмы за май 2026 — для демонстрации учёта и закрытия."""
    from decimal import Decimal as D

    from bis.models import (
        EstimateItem,
        Material,
        MaterialNorm,
        MaterialReceipt,
        MaterialUsage,
        ScheduleTask,
        WorkVolume,
    )

    items_spec = [
        ("1.1", "Устройство бетонной подготовки под фундаментную плиту, бетон B7,5", "м3", "120", "6500", True),
        ("1.2", "Армирование фундаментной плиты Пм1", "т", "85", "98000", True),
        ("1.3", "Бетонирование фундаментной плиты Пм1, бетон B25 W8", "м3", "1450", "9800", False),
        ("1.4", "Гидроизоляция фундаментной плиты", "м2", "2100", "1250", True),
        ("1.5", "Обратная засыпка пазух котлована с уплотнением", "м3", "3200", "450", True),
    ]
    items = []
    for i, (code, name, unit, qty, price, hidden) in enumerate(items_spec, 1):
        it = EstimateItem(organization_id=org_id, contract_id=contract_id, object_id=object_id, ord=i,
                          section="Раздел 1. Фундаменты", code=code, name=name, unit=unit, quantity=D(qty),
                          unit_price=D(price), requires_aosr=hidden)
        db.add(it)
        items.append(it)
    db.flush()
    plan = [(0, date(2026, 5, 4), date(2026, 5, 10)), (1, date(2026, 5, 11), date(2026, 6, 10)),
            (2, date(2026, 5, 18), date(2026, 6, 30)), (3, date(2026, 6, 15), date(2026, 7, 15)),
            (4, date(2026, 7, 1), date(2026, 8, 15))]
    for n, (idx, start, end) in enumerate(plan, 1):
        db.add(ScheduleTask(organization_id=org_id, object_id=object_id, estimate_item_id=items[idx].id,
                            name=items[idx].name, planned_start=start, planned_end=end, planned_quantity=items[idx].quantity,
                            unit=items[idx].unit, responsible_id=responsible_id, ord=n))
    mats = {}
    for key, name, unit, code in [("b25", "Бетон B25 W8 F150", "м3", "00-001"), ("b75", "Бетон B7,5", "м3", "00-002"),
                                  ("a500", "Арматура А500С", "т", "00-010"), ("membrane", "Мембрана гидроизоляционная ПВХ", "м2", "00-020"),
                                  ("sand", "Песок средней крупности", "м3", "00-030")]:
        mats[key] = Material(organization_id=org_id, name=name, unit=unit, code=code, category="Основные материалы")
        db.add(mats[key])
    db.flush()
    for idx, key, rate, loss in [(0, "b75", "1", "1.5"), (1, "a500", "1", "2"), (2, "b25", "1", "1.5"),
                                 (3, "membrane", "1.15", "0"), (4, "sand", "1.15", "0")]:
        db.add(MaterialNorm(organization_id=org_id, estimate_item_id=items[idx].id, material_id=mats[key].id,
                            rate=D(rate), loss_percent=D(loss), source="Норма компании (демо)"))
    for key, qty, price, ref, day in [("b75", "125", "5600", "УПД № 101 от 05.05.2026", 5), ("b25", "620", "7200", "УПД № 214 от 20.05.2026", 20),
                                      ("a500", "45", "72000", "УПД № 88 от 12.05.2026", 12)]:
        db.add(MaterialReceipt(organization_id=org_id, object_id=object_id, material_id=mats[key].id, date=date(2026, 5, day),
                               quantity=D(qty), price=D(price), document_ref=ref, quality_doc="Документ о качестве (демо)"))
    for key, qty, day in [("b75", "121", 8), ("b25", "625", 28)]:
        db.add(MaterialUsage(organization_id=org_id, object_id=object_id, material_id=mats[key].id, date=date(2026, 5, day),
                             quantity=D(qty), note="Отчёт прораба (демо)"))
    for idx, qty, day, status, loc in [(0, "60", 6, "confirmed", "оси 1-5/А-В"), (0, "60", 8, "confirmed", "оси 5-9/А-В"),
                                       (2, "300", 22, "confirmed", "захватка 1"), (2, "300", 28, "confirmed", "захватка 2"),
                                       (1, "40", 25, "reported", "захватки 1-2")]:
        db.add(WorkVolume(organization_id=org_id, object_id=object_id, estimate_item_id=items[idx].id,
                          date=date(2026, 5, day), quantity=D(qty), location=loc, status=status, reported_by="Демо"))
    db.flush()

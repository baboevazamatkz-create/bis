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
    for full_name, position, cp_id, nrs, role, order in people:
        person = Person(organization_id=org.id, full_name=full_name, position=position, counterparty_id=cp_id,
                        nrs_id=nrs)
        db.add(person)
        db.flush()
        db.add(Appointment(organization_id=org.id, person_id=person.id, object_id=obj.id, role=role,
                           order_number=order, order_date=date(2026, 2, 20), valid_from=date(2026, 3, 1)))

    db.add(Contract(organization_id=org.id, kind="general", number="ГП-01/2026", date=date(2026, 2, 15),
                    subject="Строительство жилого дома, корпус 1", object_id=obj.id, counterparty_id=developer.id,
                    amount=850_000_000, vat_rate=20, start_date=date(2026, 3, 1), end_date=date(2027, 12, 31)))
    db.add(Contract(organization_id=org.id, kind="subcontract", number="СП-07/2026", date=date(2026, 3, 10),
                    subject="Монолитные работы, корпус 1", object_id=obj.id, counterparty_id=sub.id,
                    amount=120_000_000, vat_rate=20, start_date=date(2026, 3, 15), end_date=date(2026, 12, 31)))
    db.add(OrgMemory(organization_id=org.id, kind="style", scope="secretary",
                     content="В письмах заказчику указывать номер договора генподряда ГП-01/2026, если письмо по корпусу 1.",
                     source="demo"))

    has_demo_norm = db.scalar(select(KnowledgeSource).where(KnowledgeSource.designation == "ДЕМО-СП 999.13330.2026"))
    if not has_demo_norm:
        data = (Path(__file__).parent / "demo_sp.txt").read_bytes()
        ingest_bytes(db, storage, embedder, org_id=None, data=data, filename="demo_sp.txt", kind="norm",
                     title="Бетонные и земляные работы (демонстрационный свод правил)",
                     designation="ДЕМО-СП 999.13330.2026", status="active")
    db.flush()
    return org

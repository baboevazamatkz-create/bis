"""Сводка дня и напоминание прорабам: факты из базы, отправка один раз в день."""

import datetime as dt

from sqlalchemy import select


def test_daily_digest_and_foreman_reminder(db, demo, monkeypatch):
    from bis.ai import conveyor as cv
    from bis.config import get_settings
    from bis.llm.gateway import get_gateway
    from bis.models import Notification, User
    from bis.rag.embeddings import get_embedder
    from bis.services import digest
    from bis.services.storage import get_storage

    foremen = []
    for name in ("Орлов Павел Игоревич", "Сидоров Пётр Ильич"):
        u = User(organization_id=demo.id, email=f"{name.split()[0]}@demo.bis", full_name=name, role="foreman", password_hash="x")
        db.add(u)
        foremen.append(u)
    db.flush()
    env = cv.Env(db=db, storage=get_storage(), gateway=get_gateway(), embedder=get_embedder(), org_id=demo.id, user=foremen[0])
    ev = cv.ingest(env, source="telegram", text="Залили бетон плиты Пм1 захватка 3 — 310 м3. Не привезли мембрану, бригада стоит.",
                   sender=foremen[0].full_name)
    cv.process(env, ev)
    db.commit()

    today = dt.date.today()
    d = digest.build(db, demo.id, today)
    assert d["done"] and d["issues"] and d["silent_foremen"] == ["Сидоров Пётр Ильич"]
    text = digest.text(d)
    assert "Сделано:" in text and "310" in text and "Не отчитались: Сидоров Пётр Ильич" in text

    s = get_settings()
    monkeypatch.setattr(s, "digest_sunday", True)
    hours = iter([s.remind_hour, s.remind_hour, s.digest_hour, s.digest_hour])

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.combine(today, dt.time(next(hours), 5))

    monkeypatch.setattr(digest.dt, "datetime", Clock)
    from bis.models import Organization

    org = db.get(Organization, demo.id)
    assert digest.daily_digest(db, org, today) == {"reminded": 1, "digest": 0}  # днём — напоминание молчащему прорабу
    assert digest.daily_digest(db, org, today)["reminded"] == 0  # второй раз за день — нет
    first = digest.daily_digest(db, org, today)
    assert first["digest"] >= 1 and digest.daily_digest(db, org, today)["digest"] == 0  # вечером — сводка, один раз
    notes = db.scalars(select(Notification).where(Notification.organization_id == demo.id,
                                                  Notification.title.like("Сводка за%"))).all()
    assert notes and "Не отчитались" in notes[0].body

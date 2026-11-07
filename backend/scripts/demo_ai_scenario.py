"""Рабочий день на объекте для демо-ленты ИИ: сигналы проходят настоящий конвейер BIS.

Письма — через приём почты (import_message), сообщения с площадки — через Telegram-коннектор (handle_update);
имитируется только транспорт (IMAP и Bot API), разбор и действия — настоящие. Решения «человека» — через review().

Запуск на демо-базе (после bis seed-demo), с теми же BIS_DATABASE_URL и BIS_STORAGE_DIR, что у сервера:
    python scripts/demo_ai_scenario.py
"""

from __future__ import annotations

import io
import json
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy import select

KZH1 = """ЖК-01-КЖ1. Фундаментная плита Пм1. Рабочая документация
Разработчик: ООО «ПроектДемо»
Спецификация
Поз. | Наименование | Ед. | Кол.
1 | Бетонирование фундаментной плиты Пм1, бетон B25 W8 | м3 | 1530
2 | Армирование фундаментной плиты Пм1 | т | 84,2
3 | Закладные детали МН-1 | шт | 48
"""
UPD = """Универсальный передаточный документ
Счет-фактура № 315 от 06.11.2026
Продавец: ООО «БетонСтрой» ИНН 7705123456
Покупатель: ООО «СтройДемо» ИНН 7701000001
Грузополучатель: ЖК-01, г. Москва, ул. Новая, з/у 7
1 | Бетон B25 W8 F150 | м3 | 40 | 8 400,00 | 336 000,00
2 | Арм. стержни 12 мм | т | 3 | 71 500,00 | 214 500,00
Всего к оплате 550 500,00"""
PASSPORT = """ДОКУМЕНТ О КАЧЕСТВЕ БЕТОННОЙ СМЕСИ № 91 от 06.11.2026
Изготовитель: ООО «БетонСтрой»
Бетонная смесь: Бетон B25 W8 F150 П4
Партия № 1106-2, объём 40 м3
Действителен до 06.05.2027"""
SURVEY = ("Точка;Xпр;Yпр;Xф;Yф;Hпр;Hф\n1;0;0;0,006;-0,004;-3,200;-3,196\n2;24;0;24,031;0,005;-3,200;-3,206\n"
          "3;24;18;23,992;18,009;-3,200;-3,203\n4;0;18;-0,004;18,007;-3,200;-3,198\n")
PROTOCOL = """ПРОТОКОЛ ИСПЫТАНИЙ № 52 от 05.11.2026
Испытательная лаборатория ООО «СтройЛаб»
Определение прочности бетона на сжатие
Конструкция: фундаментная плита Пм1, захватка 2
Требуемая прочность: 32,7 МПа (B25)
Фактическая прочность: 28,1 МПа
Заключение: не соответствует требованиям"""
LETTER = """АО «Заказчик-Девелопмент»
ИНН 7702000003
Исх. № 212/ТН от 06.11.2026
О предоставлении исполнительной документации
В соответствии с договором генподряда № ГП-01/2026 просим в течение 5 рабочих дней предоставить исполнительную документацию на армирование фундаментной плиты Пм1, захватка 3.
Также просим направить документы о качестве на бетон, поставленный 06.11.2026.
Технический директор Петров П.П."""
SUBCONTRACT = """ДОГОВОР СУБПОДРЯДА № СП-12/2026
г. Москва 05.11.2026
ООО «СтройДемо», ИНН 7701000001, именуемое в дальнейшем «Генподрядчик», в лице генерального директора Иванова И.И., и
ООО «ГидроЗащита», ИНН 7706000012, именуемое в дальнейшем «Субподрядчик», заключили настоящий договор.
1. Предмет договора
1.1. Субподрядчик выполняет работы по гидроизоляции фундаментной плиты Пм1 на объекте «Многоквартирный жилой дом, корпус 1» (ЖК-01).
2. Цена и оплата
2.1. Цена работ составляет 2 625 000 (два миллиона шестьсот двадцать пять тысяч) рублей, в т.ч. НДС 20%.
2.2. Генподрядчик оплачивает выполненные работы в течение 15 рабочих дней с даты подписания КС-2 и КС-3.
3. Сроки
3.1. Начало работ — 10.11.2026, окончание — 15.12.2026.
3.2. Субподрядчик передаёт исполнительную документацию в течение 3 рабочих дней после завершения каждой захватки.
4. Ответственность
4.1. За нарушение сроков выполнения работ Субподрядчик уплачивает неустойку 0,1% от цены работ за каждый день просрочки.
4.2. За нарушение сроков оплаты Генподрядчик уплачивает неустойку 1/300 ключевой ставки ЦБ РФ за каждый день просрочки, но не более 5% от суммы задолженности.
5. Гарантия
5.1. Гарантийный срок на работы — 5 лет с даты подписания акта приёмки объекта."""


def photo() -> bytes:
    """Иллюстрация «армирование плиты» (не фотография): сетка арматуры в перспективе."""
    from PIL import Image, ImageDraw

    w, h = 960, 640
    img = Image.new("RGB", (w, h), (92, 96, 102))
    d = ImageDraw.Draw(img)
    for i in range(0, h, 4):  # бетонная подготовка
        d.line([(0, i), (w, i)], fill=(88 + (i * 7) % 9, 92 + (i * 5) % 7, 98 + (i * 3) % 6))
    vx = w / 2
    for k in range(-14, 15):  # продольные стержни к точке схода
        x0 = vx + k * 95
        d.line([(vx + k * 18, 0), (x0, h)], fill=(122, 74, 44), width=6)
    y, step = 18, 16
    while y < h:  # поперечные стержни
        d.line([(0, y), (w, y)], fill=(138, 84, 50), width=max(3, int(y / 90)))
        step *= 1.14
        y += int(step)
    for cx, cy in ((180, 520), (600, 470), (820, 560)):  # фиксаторы
        d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill=(200, 200, 205))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


class FakeBot:
    """Имитация Bot API: отдаёт файлы по file_id и запоминает ответы бота."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.sent: list[dict[str, Any]] = []
        from bis.connectors import telegram as tg

        self.api = tg.TelegramAPI("DEMO", "https://telegram.demo", client=httpx.Client(transport=httpx.MockTransport(self._handle)))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/"):
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[-1]])
        method = path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"f/{body['file_id']}"}})
        if method == "sendMessage":
            self.sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})


def email(sender: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] = ()) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "info@stroydemo.example", subject
    msg["Message-ID"] = f"<{abs(hash(subject))}@demo.example>"
    msg.set_content(body)
    for name, data, mime in attachments:
        main, sub = mime.split("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


def run() -> dict[str, Any]:
    from bis.ai import conveyor as cv
    from bis.connectors import telegram as tg
    from bis.connectors.mailbox import import_message
    from bis.db import SessionLocal
    from bis.llm.gateway import get_gateway
    from bis.models import AIAction, Counterparty, IntakeEvent, Organization, Person, User
    from bis.rag.embeddings import get_embedder
    from bis.security import hash_password
    from bis.services.storage import get_storage

    db = SessionLocal()
    org = db.scalar(select(Organization).where(Organization.name == "ООО «СтройДемо»"))
    if db.scalar(select(IntakeEvent.id).where(IntakeEvent.organization_id == org.id).limit(1)):
        return {"skipped": "сценарий уже выполнен"}
    gw = get_gateway()

    def user(email_: str, name: str, position: str, role: str, person: str | None = None) -> User:
        u = db.scalar(select(User).where(User.email == email_))
        if u is None:
            u = User(organization_id=org.id, email=email_, full_name=name, position=position, role=role,
                     password_hash=hash_password("demo12345"))
            db.add(u)
            db.flush()
        if person:
            p = db.scalar(select(Person).where(Person.organization_id == org.id, Person.full_name == person))
            if p:
                p.user_id = u.id
        return u

    foreman = user("orlov@demo.bis", "Орлов Павел Игоревич", "Начальник участка", "foreman", person="Орлов Павел Игоревич")
    supply = user("supply@demo.bis", "Кравцова Елена Викторовна", "Инженер по снабжению", "supply")
    pto = db.scalar(select(User).where(User.email == "pto@demo.bis"))
    admin = db.scalar(select(User).where(User.email == "admin@demo.bis"))
    if not db.scalar(select(Counterparty).where(Counterparty.organization_id == org.id, Counterparty.inn == "7705123456")):
        db.add(Counterparty(organization_id=org.id, kind="supplier", name="ООО «БетонСтрой»", inn="7705123456"))
    db.commit()

    bot = FakeBot()
    update_id = [100]

    def tg_send(u: User, chat: int, **fields) -> None:
        update_id[0] += 1
        if fields.pop("link", False):
            tg.handle_update(db, bot.api, {"update_id": update_id[0], "message": {
                "message_id": update_id[0], "chat": {"id": chat, "type": "private"}, "from": {"id": chat},
                "text": f"/start {tg.link_code(u.id)}"}})
            update_id[0] += 1
        tg.handle_update(db, bot.api, {"update_id": update_id[0], "message": {
            "message_id": update_id[0], "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, **fields}})

    def mail(raw: bytes) -> None:
        import_message(db, org, raw, gateway=gw)
        db.commit()

    def env_for(u: User) -> cv.Env:
        return cv.Env(db=db, storage=get_storage(), gateway=gw, embedder=get_embedder(), org_id=org.id, user=u)

    def decide(kind: str, title_part: str, u: User, approve: bool = True, **kw) -> None:
        a = db.scalar(select(AIAction).where(AIAction.organization_id == org.id, AIAction.kind == kind,
                                             AIAction.title.contains(title_part), AIAction.status == "proposed"))
        cv.review(env_for(u), a, approve=approve, user=u, **kw)
        db.commit()

    # 1. проектировщик прислал рабочую документацию → сверка со сметой
    mail(email("ООО «ПроектДемо» <gip@proekt.example>", "РД ЖК-01-КЖ1 Фундаментная плита — выдача в производство",
               "Направляем рабочую документацию КЖ1 (изм. 0) для производства работ.",
               [("ЖК-01-КЖ1 Фундаментная плита.txt", KZH1.encode(), "text/plain")]))
    # 2. поставщик прислал УПД → приход на подтверждение; снабженец подтверждает бетон
    mail(email("ООО «БетонСтрой» <sales@beton.example>", "УПД № 315 от 06.11.2026", "Добрый день! Во вложении УПД на поставку.",
               [("УПД_315.txt", UPD.encode(), "text/plain")]))
    decide("create_receipt", "Бетон B25", supply)
    # 3. снабженец пересылает паспорт качества в Telegram → привязка к поступлению
    bot.files["pass91"] = PASSPORT.encode()
    tg_send(supply, 2002, link=True, document={"file_id": "pass91", "file_name": "Паспорт_качества_91.txt",
                                                "mime_type": "text/plain", "file_size": len(PASSPORT.encode())})
    # 4. геодезист прислал исполнительную съёмку
    mail(email("Геодезическая служба <geo@stroydemo.example>", "Исполнительная съёмка — захватка 3",
               "Съёмка арматурного каркаса плиты Пм1, захватка 3.", [("съёмка_Пм1_захватка3.csv", SURVEY.encode(), "text/csv")]))
    # 5. начальник участка пишет в Telegram: закончили армирование → объём, расход, АОСР собирается сам
    tg_send(foreman, 1001, link=True, text="Закончили армирование фундаментной плиты Пм1 захватка 3, 12 т, готово к "
                                            "освидетельствованию. Израсходовано арматуры 12 т.")
    # 6. лаборатория: протокол с несоответствием
    mail(email("ООО «СтройЛаб» <lab@stroylab.example>", "Протокол испытаний № 52", "Результаты испытаний во вложении.",
               [("Протокол_52.txt", PROTOCOL.encode(), "text/plain")]))
    # 7. фото с площадки, 8. голосовое, 9. проблема
    bot.files["ph1"] = photo()
    tg_send(foreman, 1001, caption="Захватка 3, армирование, вид сверху",
            photo=[{"file_id": "ph1", "width": 960, "height": 640, "file_size": len(bot.files["ph1"])}])
    bot.files["v1"] = b"OggS\x00demo-voice"
    tg_send(foreman, 1001, voice={"file_id": "v1", "duration": 9, "mime_type": "audio/ogg", "file_size": 16})
    tg_send(foreman, 1001, text="Не привезли мембрану, бригада гидроизоляции простаивает с 10:00.")
    # 10. письмо заказчика → секретарь; 11. договор субподряда → юрист, директор утверждает
    mail(email("АО «Заказчик-Девелопмент» <office@developer.example>", "О предоставлении исполнительной документации", LETTER))
    mail(email("ООО «ГидроЗащита» <office@gidro.example>", "Договор субподряда СП-12/2026 на подпись",
               "Направляем договор на гидроизоляцию.", [("Договор_СП-12-2026.txt", SUBCONTRACT.encode(), "text/plain")]))
    decide("import_contract", "Новый договор", admin)
    _ = pto
    events = db.scalar(select(IntakeEvent.id).where(IntakeEvent.organization_id == org.id).limit(1))
    db.close()
    return {"ok": bool(events), "bot_replies": len(bot.sent)}


if __name__ == "__main__":
    print(run())

"""Запись снимка демо-данных с работающего backend BIS для статической демо-страницы.

Запуск (на свежей демо-базе: bis migrate && bis seed-demo && bis run-jobs):
    python scripts/record_demo.py http://localhost:8000 ../frontend/demo-snapshot.json

Скрипт выполняет типовые действия через настоящий API (АОСР, письмо, КС-2/КС-3, М-29, акт списания,
схема, сверка ПД, чат) и сохраняет ответы; демо-страница воспроизводит их без сервера.
"""

from __future__ import annotations

import html
import io
import json
import sys
from pathlib import Path

import httpx

KZH_SPEC = """ЖК-01-КЖ1. Фундаментная плита Пм1
Спецификация
Поз. | Наименование | Ед. | Кол.
1 | Бетонирование фундаментной плиты Пм1, бетон B25 W8 | м3 | 1530
2 | Армирование фундаментной плиты Пм1 | т | 84,2
3 | Закладные детали МН-1 | шт | 48
"""
SURVEY = ("Точка;Xпр;Yпр;Xф;Yф;Hпр;Hф\n1;0;0;0,008;-0,005;-3,200;-3,195\n2;24;0;24,012;0,004;-3,200;-3,214\n"
          "3;24;18;23,990;18,021;-3,200;-3,203\n4;0;18;-0,003;18,006;-3,200;-3,199\n")
PERIOD = {"period_start": "2026-05-01", "period_end": "2026-05-31"}
LISTS = ["objects", "counterparties", "contracts", "persons", "appointments", "tasks", "obligations", "estimate-items",
         "schedule", "volumes", "materials", "material-norms", "material-receipts", "material-usage"]
CHAT = [
    "Какая допустимая толщина слоя обратной засыпки?",
    "Когда можно снимать несущую опалубку?",
    "Что просрочено?",
    "Что просрочено по договорам?",
    "Какие риски в договоре ГП-01/2026?",
    "Покажи план-факт по корпусу 1",
    "Какой перерасход материалов за май 2026?",
    "Покажи остатки материалов на объекте",
    "Что можно закрыть в КС-2 за май 2026 по договору ГП-01/2026?",
    "Проверь комплектность проектной документации",
    "Покажи поручения",
]


def docx_html(data: bytes) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    out = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = html.escape(p.text)
            align = {1: "center", 2: "right", 3: "justify"}.get(int(p.alignment) if p.alignment is not None else 0, "left")
            bold = any(r.bold for r in p.runs)
            out.append(f'<p style="text-align:{align}">{"<b>" if bold else ""}{text or "&nbsp;"}{"</b>" if bold else ""}</p>')
        elif tag == "tbl":
            rows = []
            for row in Table(child, d).rows:
                cells, prev = [], None
                for c in row.cells:
                    if c._tc is prev:
                        continue
                    prev = c._tc
                    cells.append(f"<td>{html.escape(c.text)}</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            out.append("<table>" + "".join(rows) + "</table>")
    return "".join(out)


def xlsx_html(data: bytes) -> str:
    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True).active
    merged = {}
    skip = set()
    for rng in ws.merged_cells.ranges:
        merged[(rng.min_row, rng.min_col)] = rng.max_col - rng.min_col + 1
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    skip.add((r, c))
    rows = []
    for r in range(1, ws.max_row + 1):
        cells = []
        empty = True
        for c in range(1, ws.max_column + 1):
            if (r, c) in skip:
                continue
            v = ws.cell(r, c).value
            if v is not None:
                empty = False
            text = "" if v is None else (f"{v:,.2f}".replace(",", " ") if isinstance(v, float) else str(v))
            span = merged.get((r, c))
            attr = f' colspan="{span}"' if span else ""
            style = ' style="border:none;text-align:left"' if span and span > 3 else ""
            cells.append(f"<td{attr}{style}>{html.escape(text)}</td>")
        if not empty:
            rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def main(base: str, out: Path) -> None:
    c = httpx.Client(base_url=base.rstrip("/") + "/api", timeout=120)
    token = c.post("/auth/login", json={"email": "admin@demo.bis", "password": "demo12345"}).json()["access_token"]
    c.headers["Authorization"] = f"Bearer {token}"

    def get(path):
        r = c.get(path)
        r.raise_for_status()
        return r.json()

    def post(path, **kw):
        r = c.post(path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    objects = get("/objects")
    obj = objects[0]["id"]
    contract = next(x for x in get("/contracts") if x["number"] == "ГП-01/2026")

    # проектная документация объекта (входит в исходное состояние демо)
    pd_doc = None
    for name, text in [("ЖК-01-ПЗ Пояснительная записка.txt", "Пояснительная записка"),
                       ("ЖК-01-АР Архитектурные решения.txt", "Объёмно-планировочные и архитектурные решения"),
                       ("ЖК-01-КР Конструктивные решения.txt", "Конструктивные решения"),
                       ("ЖК-01-ИОС1 Электроснабжение.txt", "Система электроснабжения"),
                       ("ЖК-01-КЖ1 Фундаментная плита.txt", KZH_SPEC)]:
        d = post("/documents/upload", data={"kind": "project_doc", "object_id": obj, "title": name.rsplit(".", 1)[0]},
                 files={"file": (name, text.encode(), "text/plain")})
        if "КЖ1" in name:
            pd_doc = d

    snap: dict = {"get": {}, "lists": {}, "actions": {}, "previews": {}, "chat": [], "chunks": [],
                  "demo_period": [PERIOD["period_start"], PERIOD["period_end"]], "demo_contract_id": contract["id"],
                  "pd_doc_id": pd_doc["id"] if pd_doc else None}
    for base_name in LISTS:
        snap["lists"][base_name] = get(f"/{base_name}?limit=500")
    for path in ["/auth/me", "/agents", "/tools", "/documents/kinds", "/knowledge/kinds", "/documents/representative-roles",
                 "/jobs", "/templates", "/voice/status", "/health", "/knowledge/sources", "/audit"]:
        snap["get"][path] = get(path)
    for o in objects:
        for suffix in ("plan-fact", "materials/balance", "pd-completeness"):
            snap["get"][f"/objects/{o['id']}/{suffix}"] = get(f"/objects/{o['id']}/{suffix}")
    snap["documents"] = get("/documents?limit=500")
    snap["notifications"] = get("/notifications?limit=200")
    snap["memory"] = get("/memory")
    snap["users"] = get("/users")
    snap["org"] = get("/org")

    # база знаний для поиска на клиенте
    for src in snap["get"]["/knowledge/sources"]:
        for ch in get(f"/knowledge/sources/{src['id']}/chunks?limit=500"):
            citation = src["designation"] or src["title"]
            if src["designation"] and ch["clause"]:
                citation += f", п. {ch['clause']}"
            elif src["designation"] and ch["heading"]:
                citation += f", {ch['heading']}"
            elif ch.get("page"):
                citation += f", стр. {ch['page']}"
            snap["chunks"].append({"chunk_id": f"{src['id']}:{ch['ord']}", "source_id": src["id"], "title": src["title"],
                                   "kind": src["kind"], "designation": src["designation"], "status": src["status"],
                                   "citation": citation, **ch})

    # действия
    a = snap["actions"]
    aosr_body = {"object_id": obj, "works": "Армирование фундаментной плиты Пм1 в осях 1-5/А-Г",
                 "project_docs": "ЖК-01-КЖ1, листы 3-7, ООО «ПроектДемо»",
                 "materials": ["Арматура А500С d12, сертификат качества № 123 от 01.05.2026"],
                 "executive_docs": ["Исполнительная схема № 5"], "work_start": "2026-05-11", "work_end": "2026-05-25",
                 "norms": ["ДЕМО-СП 999.13330.2026, п. 4.2.1-4.2.3"], "next_works": "Бетонирование фундаментной плиты Пм1",
                 "act_date": "2026-05-26"}
    a["aosr_check"] = post("/documents/aosr/check", json=aosr_body)
    a["aosr_doc"] = post("/documents/aosr", json=aosr_body)
    a["letter_doc"] = post("/documents/letter", json={
        "addressee": ["Генеральному директору", "АО «Заказчик-Девелопмент»", "А.В. Смирнову"],
        "subject": "О направлении исполнительной документации", "greeting": "Уважаемый Алексей Викторович!",
        "body": ["В соответствии с п. 4.1 договора генерального подряда № ГП-01/2026 направляем исполнительную документацию "
                 "на устройство фундаментной плиты Пм1 за май 2026 г.", "Просим рассмотреть и подписать в установленный срок."],
        "attachments": ["АОСР № 1 на 2 л. в 1 экз."], "object_id": obj, "contract_id": contract["id"]})
    a["closing_check"] = post(f"/contracts/{contract['id']}/closing/check", json=PERIOD)
    a["ks2"] = post(f"/contracts/{contract['id']}/closing/ks2", json=PERIOD)
    a["m29_preview"] = post("/materials/m29/preview", json={"object_id": obj, **PERIOD})
    a["m29_doc"] = post("/materials/m29", json={"object_id": obj, **PERIOD})
    a["writeoff_doc"] = post("/materials/write-off", json={"object_id": obj, **PERIOD,
                                                            "commission": ["Главный инженер Сидоров О.П.", "Прораб Орлов П.И."]})
    a["survey_points"] = post("/schemes/survey", files={"file": ("survey.csv", SURVEY.encode(), "text/csv")})["points"]
    a["scheme_doc"] = post("/schemes", json={
        "title": "Исполнительная схема фундаментной плиты Пм1 в осях 1-5/А-Г", "code": "ЖК-01-ИГС-01",
        "surveyor": "Геодезист Сидоров А.А.", "object_id": obj, "points": a["survey_points"],
        "x_axes": [{"name": str(i + 1), "coord": i * 6} for i in range(5)],
        "y_axes": [{"name": n, "coord": v} for n, v in zip("АБВГ", [0, 6, 12, 18], strict=True)],
        "contour": [[p["design_x"], p["design_y"]] for p in a["survey_points"]], "date": "20.05.2026"})
    if pd_doc:
        a["specification"] = post(f"/documents/{pd_doc['id']}/specification")
        a["compare_estimate"] = post(f"/documents/{pd_doc['id']}/compare-estimate", json={"contract_id": contract["id"]})
    for job in ("deadlines", "plan_fact"):
        a[f"job_{job}"] = post(f"/jobs/{job}/run")
    for q in CHAT:
        snap["chat"].append({"question": q, "reply": post("/chat", json={"message": q})})

    # предпросмотры файлов
    docs = [a["aosr_doc"], a["letter_doc"], a["ks2"]["ks2"], a["ks2"]["ks3"], a["m29_doc"], a["writeoff_doc"],
            a["scheme_doc"], *snap["documents"]]
    for d in docs:
        if not d.get("file_name") or d["id"] in snap["previews"]:
            continue
        if d["kind"] == "scheme":
            snap["previews"][d["id"]] = {"type": "svg", "content": c.get(f"/documents/{d['id']}/preview").text}
            continue
        raw = c.get(f"/documents/{d['id']}/file").content
        name = d["file_name"].lower()
        if name.endswith(".docx"):
            content = docx_html(raw)
        elif name.endswith(".xlsx"):
            content = xlsx_html(raw)
        elif name.endswith((".txt", ".eml")):
            content = f"<pre style='white-space:pre-wrap'>{html.escape(raw.decode('utf-8', 'replace'))}</pre>"
        else:
            continue
        snap["previews"][d["id"]] = {"type": "html", "content": content}
    out.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    print(f"Снимок: {out} ({out.stat().st_size // 1024} КБ), документов {len(snap['documents'])}, "
          f"ответов чата {len(snap['chat'])}, фрагментов {len(snap['chunks'])}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))

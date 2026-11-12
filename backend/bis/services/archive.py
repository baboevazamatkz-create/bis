"""Загрузка архива организации: папки с исполнительной документацией, договорами, письмами, проектом.

Каждый файл: текст (PDF, DOCX, XLSX, EML, TXT; сканы — OCR; DXF — надписи чертежа) → тип документа (правила,
при сомнении — модель) → реестр документов объекта → база знаний (умный поиск, образцы для ИИ-специалистов).
Договоры разбираются на обязательства; письма регистрируются без поручений (это история, а не новые входящие).
Повторная загрузка того же файла пропускается (по содержимому).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bis.ai import understanding as und
from bis.models import ConstructionObject, Contract, Document
from bis.rag.parsers import Page, UnsupportedFormat, parse_file
from bis.services.documents import save_document
from bis.services.storage import Storage

# Документы, которых нет среди входящих с площадки, но много в архиве
ARCHIVE_RULES: list[tuple[str, str]] = [
    ("aosr", r"акт\w*\s+освидетельствовани\w+\s+скрыт|\bаоср\b"),
    ("aook", r"освидетельствовани\w+\s+ответственн\w+\s+конструкц|\bаоок\b"),
    ("scheme", r"исполнительн\w+\s+(?:геодезическ\w+\s+)?схем|\bигс\b|исп\.?\s*схем|\bис[-\s№]*\d"),
    ("ks2", r"\bкс-?2\b|акт\w*\s+о\s+при[её]мке\s+выполненных\s+работ"),
    ("ks3", r"\bкс-?3\b|справк\w+\s+о\s+стоимости\s+выполненных"),
    ("order", r"^\s*приказ\b|\bприказ\s*№"),
    ("work_journal", r"общ\w+\s+журнал\s+работ|журнал\s+(?:бетонных|сварочных|выполнения)\s+работ"),
    ("certificate", r"^\s*(?:сертификат\b|паспорт\s+(?:качества|на\s)|деклараци[яи]\s+о\s+соответствии)"),
    ("test_protocol", r"^\s*протокол\s+(?:№|испытан|измерен|проверки)"),
    ("project_doc", r"^\s*(?:рабочая\s+документация|проектная\s+документация|рд\s*(?:№|[-_\d]))"),
]
KIND = {"aosr": "aosr", "aook": "aook", "scheme": "scheme", "ks2": "ks2", "ks3": "ks3", "order": "order",
        "work_journal": "work_journal", "contract": "contract", "letter": "incoming_letter", "project_doc": "project_doc",
        "upd": "supply_doc", "certificate": "certificate", "test_protocol": "test_protocol", "survey": "survey",
        "site_photo": "photo", "foreman_report": "other", "unknown": "other"}
KNOWLEDGE = {"contract": "contract", "letter": "letter", "project_doc": "project_doc"}
TITLES = {"aosr": "АОСР", "aook": "АООК", "scheme": "Исполнительная схема", "ks2": "КС-2", "ks3": "КС-3", "order": "Приказ",
          "work_journal": "Журнал работ"}
SKIP = re.compile(r"(^~\$|^\.|thumbs\.db$|desktop\.ini$)", re.IGNORECASE)


def contract_kind(text: str) -> str:
    """Вид договора — по его названию («ДОГОВОР ПОДРЯДА №…», «договор поставки»), а не по любому слову в начале:
    в договоре подряда «проектная документация» упоминается в предмете, но он от этого не проектный."""
    head = text[:3000].lower().replace("ё", "е")
    m = re.search(r"договор[а-я]*\s+(?:№\s*\S+\s+)?(?:на\s+)?([а-я\- ,]{3,80})", head)
    title = m.group(1) if m else head[:300]
    if "поставк" in title:
        return "supply"
    if "субподряд" in title:
        return "subcontract"
    if re.search(r"проектн|проектир|изыскан", title) and "строительн" not in title:
        return "design"
    return "general"


def _pages(data: bytes, name: str, mime: str | None) -> tuple[list[Page], str]:
    """Текст по страницам и как получен; колонки (реквизиты сторон, подписи) разложены по одной."""
    from bis.rag.columns import split_columns

    pages, how = _raw_pages(data, name, mime)
    return [Page(p.number, split_columns(p.text), p.warning) for p in pages], how


def reading_report(pages, method):
    empty = [p.number for p in pages if p.number is not None and not p.text.strip()]
    sparse = [p.number for p in pages if p.number is not None and len(p.text.strip()) < 50]
    return {'segments': len(pages), 'with_text': sum(bool(p.text.strip()) for p in pages), 'sparse_pages': sparse,
            'empty_pages': empty, 'complete': bool(pages) and not sparse and not any(p.warning for p in pages) and any(p.text.strip() for p in pages),
            'method': method, 'warnings': list(dict.fromkeys(p.warning for p in pages if p.warning))}


def _raw_pages(data: bytes, name: str, mime: str | None) -> tuple[list[Page], str]:
    """Текст по страницам и как получен: parsed | ocr | dxf | none. Номер страницы — для ссылок «стр. N»."""
    ext = Path(name).suffix.lower()
    if ext == ".dxf":
        try:
            import io

            import ezdxf

            doc = ezdxf.read(io.StringIO(data.decode("cp1251", errors="ignore") if b"\r\n" in data[:200] else
                                         data.decode("utf-8", errors="ignore")))
            texts = [e.dxf.text for e in doc.modelspace().query("TEXT")] + \
                    [e.plain_text() for e in doc.modelspace().query("MTEXT")]
            return [Page(None, "\n".join(t for t in texts if t))], "dxf"
        except Exception:  # noqa: BLE001 — чертёж без читаемых надписей
            return [], "none"
    if ext in (".dwg",):
        return [], "none"
    if (mime or "").startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
        recognized = _ocr_pages(data, mime or mimetypes.guess_type(name)[0])
        return ([Page(i + 1, t) for i, t in enumerate(recognized)] or
                [Page(1, '', getattr(recognized, 'error', None) or 'OCR не извлёк текст изображения')]), "ocr"
    try:
        parsed = parse_file(data, name, mime)
    except UnsupportedFormat:
        return [], "none"
    if ext in ('.xlsx', '.xlsm'):
        warnings = []
        if parsed.meta.get('uncached_formulas'):
            warnings.append(f"Формул без сохранённых результатов: {len(parsed.meta['uncached_formulas'])}")
        if parsed.meta.get('truncated_sheets'):
            warnings.append('Не полностью прочитаны листы: ' + ', '.join(parsed.meta['truncated_sheets']))
        if warnings:
            for page in parsed.pages:
                page.warning = '; '.join(warnings)
        return parsed.pages, 'parsed'
    if ext == '.docx' and not parsed.text.strip():
        import zipfile

        with zipfile.ZipFile(__import__('io').BytesIO(data)) as package:
            images = [n for n in package.namelist() if n.startswith('word/media/') and
                      Path(n).suffix.lower() in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp')]
            pages = []
            for image in images:
                recognized = _ocr_pages(package.read(image), mimetypes.guess_type(image)[0])
                pages.extend(Page(None, text) for text in recognized)
                if not recognized:
                    pages.append(Page(None, '', getattr(recognized, 'error', None) or f'Не прочитано изображение {image}'))
        return pages or [Page(None, '', 'В DOCX нет извлечённого текста; преобразуйте скан в PDF')], 'ocr_docx'
    if ext in ('.doc', '.rtf') and not parsed.text.strip():
        return [Page(None, '', 'В DOC/RTF нет текстового содержимого. Для распознавания встроенных сканов сохраните документ в PDF.')], 'parsed'
    if parsed.meta.get("needs_ocr") or not parsed.text.strip():
        scanned = _ocr_pages(data, mime or "application/pdf")
        if any(t.strip() for t in scanned):
            # Keep native text when OCR fails to read an otherwise readable page.
            size = max(len(parsed.pages), len(scanned))
            pages = []
            for i in range(size):
                native = parsed.pages[i].text if i < len(parsed.pages) else ""
                recognized = scanned[i] if i < len(scanned) else ""
                from bis.rag.parsers import text_quality

                pages.append(Page(i + 1, native if len(native.strip()) >= 50 and text_quality(native) >= 0.5 else recognized or native))
            return pages, "ocr"
        error = getattr(scanned, 'error', None) or 'OCR не извлёк текст страницы'
        for page in parsed.pages:
            if page.number in parsed.meta.get('ocr_pages', []) or not page.text.strip():
                page.warning = error
    return parsed.pages, "parsed"


def _ocr_pages(data: bytes, mime: str | None) -> list[str]:
    from bis import ocr

    class OCRResult(list):
        error: str | None = None

    result = OCRResult()
    if not ocr.available():
        result.error = 'OCR не подключён: настройте распознавание сканов'
        return result
    try:
        return ocr.recognize_pages(data, mime)
    except ocr.OCRError as exc:
        result.error = str(exc)[:500]
        return result


def detect_type(gateway, text: str, name: str) -> tuple[str, str]:
    """(тип, кем определён)."""
    # тип — по названию файла и заголовку документа (первые строки), а не по упоминаниям в тексте:
    # в договоре встречаются «КС-2» и «АОСР», в письме — «исполнительная схема»
    if re.match(r'^\s*договор(?:\s+(?:суб)?подряда|\s*№|\s+(?:поставки|оказания))', text, re.I):
        return 'contract', 'rules'
    head = "\n".join(ln for ln in text.splitlines() if ln.strip())[:300]
    probe = (Path(name).stem.replace("_", " ") + "\n" + head).lower()
    if re.search(r"\bисх\.?\s*№|^\s*вх\.?\s*№", head, re.IGNORECASE | re.MULTILINE):
        return "letter", "rules"
    for t, pattern in ARCHIVE_RULES:
        if re.search(pattern, probe, re.IGNORECASE | re.MULTILINE):
            return t, "rules"
    if Path(name).suffix.lower() in (".dwg", ".dxf"):
        return "scheme", "rules"
    t, _, by = und.classify(gateway, text, name, "document")
    return t, by


def _object_for(db: Session, org_id: uuid.UUID, hint: str) -> uuid.UUID | None:
    """Объект по названию папки или тексту: шифр или значимые слова названия; объект один — он."""
    from bis.services import works as wsvc

    objs = db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == org_id)).all()
    if len(objs) == 1:
        return objs[0].id
    low = hint.lower()
    by_code = [o for o in objs if o.code and o.code.lower() in low]
    if len(by_code) == 1:
        return by_code[0].id
    stems = wsvc._stems(hint)
    named = [o for o in objs if (own := wsvc._stems(o.name) - wsvc._stems("жилой дом корпус объект комплекс здание"))
             and len(own & stems) / len(own) >= 0.6]
    return named[0].id if len(named) == 1 else None


def _ours(db: Session, org_id: uuid.UUID, text: str) -> bool:
    """Письмо от нашей организации: её название в шапке (до адресата)."""
    from bis.models import Organization

    org = db.get(Organization, org_id)
    if org is None or not org.name:
        return False
    core = re.sub(r"^(ооо|ао|пао|зао|оао|ип)\s+", "", org.name.lower().replace("«", "").replace("»", "").replace('"', "")).strip()
    head = []
    for ln in text.splitlines():
        if re.search(r"директору|начальнику|руководителю|заместителю|главному|^\s*кому\b|уважаем", ln, re.IGNORECASE):
            break
        head.append(ln)
    top = " ".join(head[:6]).lower().replace("«", "").replace("»", "").replace('"', "")
    return bool(core) and core in top


def assign_single_object(db: Session, org_id: uuid.UUID) -> int:
    """У организации один объект — документы и договоры без объекта относятся к нему (архив загрузили раньше объекта)."""
    objs = db.scalars(select(ConstructionObject.id).where(ConstructionObject.organization_id == org_id).limit(2)).all()
    if len(objs) != 1:
        return 0
    n = db.execute(update(Document).where(Document.organization_id == org_id, Document.object_id.is_(None),
                                          Document.kind.notin_(["order"])).values(object_id=objs[0])).rowcount
    n += db.execute(update(Contract).where(Contract.organization_id == org_id, Contract.object_id.is_(None))
                    .values(object_id=objs[0])).rowcount
    return n


def repair_archive(db: Session) -> dict[str, int]:
    """Поправить загруженное раньше: объект (если он один) и дату документа из текста вместо даты загрузки."""
    from sqlalchemy import cast
    from sqlalchemy.types import Date

    from bis.models import KnowledgeChunk, KnowledgeSource, Organization

    fixed = {"objects": 0, "dates": 0, "letters": 0}
    for org_id in db.scalars(select(Organization.id)).all():
        fixed["objects"] += assign_single_object(db, org_id)
    docs = db.scalars(select(Document).where(Document.source == "archive", Document.date == cast(Document.created_at, Date),
                                             Document.data["date_checked"].astext.is_(None)).limit(5000)).all()
    for doc in docs:
        text = db.scalar(select(KnowledgeChunk.content).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                         .where(KnowledgeSource.document_id == doc.id).order_by(KnowledgeChunk.ord).limit(1)) or ""
        doc.date = doc_date(text, (doc.data or {}).get("detected_type", ""))
        doc.data = {**(doc.data or {}), "date_checked": True}
        fixed["dates"] += 1
    # исходящие письма организации, загруженные раньше как входящие
    letters = db.scalars(select(Document).where(Document.source == "archive", Document.kind == "incoming_letter",
                                                Document.data["direction_checked"].astext.is_(None)).limit(5000)).all()
    for doc in letters:
        text = db.scalar(select(KnowledgeChunk.content).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                         .where(KnowledgeSource.document_id == doc.id).order_by(KnowledgeChunk.ord).limit(1)) or ""
        if _ours(db, doc.organization_id, text):
            doc.kind = "letter"
            fixed["letters"] += 1
        doc.data = {**(doc.data or {}), "direction_checked": True}
    return fixed


_MONTHS = {m: i for i, m in enumerate(["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр",
                                        "октябр", "ноябр", "декабр"], 1)}
_DMY = r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})"
_LONG = r"«?(\d{1,2})»?\s+(январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*\s+(\d{4})"


def _date(d: str, m: str, y: str) -> dt.date | None:
    try:
        year = int(y) + (2000 if len(y) == 2 else 0)
        month = int(m) if m.isdigit() else _MONTHS[m]
        out = dt.date(year, month, int(d))
    except (ValueError, KeyError):
        return None
    return out if dt.date(1990, 1, 1) <= out <= dt.date.today() + dt.timedelta(days=366) else None


def doc_date(text: str, dtype: str) -> dt.date | None:
    """Дата документа из текста: для актов — окончание работ, для писем и договоров — «от …» в шапке."""
    head = text[:1500]
    patterns = []
    if dtype in ("aosr", "aook"):
        patterns.append(r"окончани\w*(?:\s+работ\w*)?[^0-9]{0,30}" + _DMY)
    patterns += [r"\bот\s+" + _DMY, r"\bот\s+" + _LONG, r"(?:дата\s+(?:выдачи|регистрации)|выдан\w*|составлен\w*)[^0-9]{0,20}" + _DMY]
    # An expiry date is not the issue date. Bare dates in journals are work entries.
    if dtype not in ("certificate", "work_journal", "test_protocol"):
        patterns += [r"\bдата[^0-9]{0,20}" + _DMY, _LONG, _DMY]
    for pattern in patterns:
        for m in re.finditer(pattern, head if pattern not in (_LONG, _DMY) else text[:600], re.IGNORECASE):
            got = _date(*m.groups()[-3:])
            if got:
                return got
    return None


def import_file(db: Session, storage: Storage, gateway, embedder, org_id: uuid.UUID, *, data: bytes, name: str,
                mime: str | None = None, object_id: uuid.UUID | None = None, folder: str = "",
                analyze_contracts: bool = True, actor: str = "system:archive") -> dict[str, Any]:
    if SKIP.search(Path(name).name):
        return {"file": name, "status": "skipped", "reason": "служебный файл"}
    digest = hashlib.sha256(data).hexdigest()
    dup = db.scalar(select(Document.id).where(Document.organization_id == org_id, Document.data["sha256"].astext == digest))
    if dup:
        return {"file": name, "status": "duplicate", "document_id": str(dup)}
    mime = mime or mimetypes.guess_type(name)[0]
    pages, how = _pages(data, name, mime)
    text = "\n".join(p.text for p in pages)
    dtype, by = detect_type(gateway, text, name)
    object_id = object_id or _object_for(db, org_id, f"{folder} {name} {text[:1500]}")
    kind = KIND.get(dtype, "other")
    if dtype == "letter" and _ours(db, org_id, text):
        kind = "letter"  # исходящее: наша организация — отправитель
    title = Path(name).stem
    if dtype in TITLES and not title.lower().startswith(TITLES[dtype].lower()[:3]):
        title = f"{TITLES[dtype]}: {title}"
    doc = save_document(db, storage, org_id=org_id, kind=kind, title=title[:900],
                        data={"sha256": digest, "archive": True, "detected_type": dtype, "detected_by": by, "text_from": how,
                              "reading": reading_report(pages, how),
                              "folder": folder[:300]},
                        file_bytes=data, file_name=Path(name).name, created_by=actor, object_id=object_id,
                        mime=mime or "application/octet-stream")
    doc.status, doc.source = "registered", "archive"
    doc.date = doc.date or doc_date(text, dtype)
    result: dict[str, Any] = {"file": name, "status": "ok", "type": dtype, "kind": kind, "document_id": str(doc.id),
                              "date": doc.date.isoformat() if doc.date else None,
                              "object_id": str(object_id) if object_id else None, "text": how, "chars": len(text)}
    card = None
    if text.strip():
        from bis.brain.card import build_card

        card = build_card(gateway, text, dtype, name)  # «Синапс»: карточка — шапка для поиска, номер, дата, ссылки
        result["card"] = card.title
    if text.strip():
        from bis.rag.ingest import ingest_pages

        try:
            with db.begin_nested():
                ingest_pages(db, embedder, org_id=org_id, pages=[p for p in pages if p.text.strip()], title=doc.title,
                             kind=KNOWLEDGE.get(dtype, "org_document"), document_id=doc.id,
                             context=card.title if card else None)
            result["knowledge"] = True
        except Exception as e:  # noqa: BLE001 — документ в реестре есть, поиск по нему — нет
            result["knowledge_error"] = str(e)[:200]
    if card is not None:
        from bis.brain import links
        from bis.brain.build import apply_card

        apply_card(db, embedder, doc, card, reembed=False)
        result["links"] = len(links.rebuild(db, doc, card))
    if dtype == "contract" and analyze_contracts and text.strip():
        from bis.ai.conveyor import contract_role
        from bis.models import Organization
        from bis.services.contracts import analyze_contract

        ckind = contract_kind(text)
        c = Contract(organization_id=org_id, kind=ckind, our_role=contract_role(text, db.get(Organization, org_id), ckind),
                     number="б/н", object_id=object_id, document_id=doc.id)
        db.add(c)
        db.flush()
        doc.contract_id = c.id
        from bis.brain.parties import link_contract

        result["parties"] = link_contract(db, c, text)  # стороны → контрагенты, заказчик → объект
        try:
            with db.begin_nested():
                extracted, _, added = analyze_contract(db, storage, gateway, c, actor="agent:lawyer", text=text)
            c.number = (extracted.number or c.number)[:100]
            result.update(contract_id=str(c.id), obligations=added)
        except Exception as e:  # noqa: BLE001
            result["contract_error"] = str(e)[:200]
    return result

"""Зрение «Эрудита»: схемы, чертежи, фото документов — что изображено на листе.

Лист (страница PDF или картинка) → модель зрения: вид листа, штамп (шифр, лист, наименование, изм.), конструкции,
оси, отметки, отклонения (для исполнительных схем), примечания. Результат — в карточку и в поиск («Описание листа»).
Без модели зрения ничего не ломается: листы досчитаются, когда зрение включат.
"""

from __future__ import annotations

import base64
import io
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
VISION_KINDS = {"scheme", "project_doc", "survey", "site_photo", "photo"}
MAX_SHEETS = 8


class TitleBlock(BaseModel):
    code: str | None = Field(None, description="Шифр (обозначение) из штампа: «17-2026-КЖ0»")
    sheet: str | None = Field(None, description="Номер листа")
    sheets_total: str | None = None
    title: str | None = Field(None, description="Наименование листа/изображения")
    revision: str | None = Field(None, description="Номер изменения, если есть")
    organization: str | None = None
    stage: str | None = Field(None, description="Стадия: П, Р, ИД")
    date: str | None = None


class Deviation(BaseModel):
    point: str = Field(description="Точка или ось: «1/А», «т3»")
    plan_mm: str | None = Field(None, description="Отклонение в плане, мм, как подписано")
    height_mm: str | None = Field(None, description="Отклонение по высоте, мм")


class Sheet(BaseModel):
    sheet_type: str = Field(description="исполнительная схема | план | разрез | узел | фасад | спецификация | "
                                        "схема армирования | документ (скан текста) | фото | другое")
    description: str = Field(description="Что изображено, 2–4 предложения: конструкция, оси, главное содержание")
    title_block: TitleBlock = Field(default_factory=TitleBlock)
    elements: list[str] = Field(default_factory=list, description="Конструкции и элементы с марками: «плита Фм1», «К-1»")
    axes: list[str] = Field(default_factory=list, description="Оси на листе: «1-6», «А-Г»")
    levels: list[str] = Field(default_factory=list, description="Отметки: «-3.300»")
    materials: list[str] = Field(default_factory=list, description="Материалы и марки, если подписаны")
    deviations: list[Deviation] = Field(default_factory=list, description="Только для исполнительных схем")
    notes: list[str] = Field(default_factory=list, description="Примечания и важные надписи на листе")


PROMPT = ("Ты инженер ПТО и читаешь лист строительной документации (чертёж, исполнительную схему, спецификацию или скан). "
          "Опиши, что на листе, и перепиши из штампа шифр, номер листа, наименование, изменение, организацию, стадию. "
          "Оси, отметки, марки конструкций и материалов — как подписаны. Для исполнительной схемы — отклонения по точкам "
          "(в плане и по высоте, мм). Только то, что видно на листе; не придумывай.")


class VisionUnavailable(Exception):
    """Модель зрения настроена, но не отвечает (нет доступа, 403): документы ждут, а не помечаются ошибкой."""


_DOWN: dict[str, object] = {"until": None, "reason": None}
_AUTH = ("403", "401", "Forbidden", "PermissionDenied", "Permission denied", "Не подключена модель зрения")


def available(gateway) -> bool:
    import time

    if _DOWN["until"] and time.time() < float(_DOWN["until"]):
        return False
    try:
        return bool(gateway and gateway.has_capability("vision", "vision"))
    except Exception:  # noqa: BLE001
        return False


def status(gateway) -> dict[str, object]:
    ok = available(gateway)
    return {"available": ok, "reason": None if ok else (_DOWN["reason"] or "модель зрения не настроена")}


def sheet_images(data: bytes, mime: str | None, name: str, limit: int = MAX_SHEETS) -> list[dict[str, str]]:
    """Листы как картинки PNG/JPEG для модели: страницы PDF (рендер 1600 px по длинной стороне) или само изображение."""
    mime = (mime or "").lower()
    low = name.lower()
    if "pdf" in mime or low.endswith(".pdf"):
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        out = []
        for i in range(min(len(pdf), limit)):
            page = pdf[i]
            w, h = page.get_size()
            scale = 1600 / max(w, h)
            img = page.render(scale=scale).to_pil()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=85)
            out.append({"mime": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()})
        return out
    if mime.startswith("image/") or low.endswith((".jpg", ".jpeg", ".png")):
        return [{"mime": mime or "image/jpeg", "data": base64.b64encode(data).decode()}]
    return []


def read_sheets(gateway, images: list[dict[str, str]]) -> list[Sheet]:
    import time

    from bis.llm.extract import extract

    out: list[Sheet] = []
    for img in images:
        res = extract(gateway, Sheet, "", images=[img], task="vision", instructions=PROMPT)
        if res.data is not None and res.provider not in (None, "offline"):
            out.append(res.data)
            continue
        errors = " ".join(res.errors)
        if not out and any(k in errors for k in _AUTH):  # нет доступа к модели — не мучить её 30 минут
            _DOWN["until"], _DOWN["reason"] = time.time() + 1800, errors[:300]
            raise VisionUnavailable(errors[:300])
    return out


def sheet_text(i: int, s: Sheet) -> str:
    """Описание листа для поиска: находится по «что на листе», а не только по надписям."""
    tb = s.title_block
    head = f"Лист {tb.sheet or i}" + (f" «{tb.title}»" if tb.title else "") + (f", шифр {tb.code}" if tb.code else "") + \
        (f", изм. {tb.revision}" if tb.revision else "")
    parts = [f"{head} ({s.sheet_type}). {s.description}"]
    if s.elements:
        parts.append("Конструкции: " + ", ".join(s.elements))
    if s.axes:
        parts.append("Оси: " + ", ".join(s.axes))
    if s.levels:
        parts.append("Отметки: " + ", ".join(s.levels))
    if s.materials:
        parts.append("Материалы: " + ", ".join(s.materials))
    if s.deviations:
        parts.append("Отклонения: " + "; ".join(f"{d.point}: в плане {d.plan_mm or '—'} мм, по высоте {d.height_mm or '—'} мм"
                                                 for d in s.deviations))
    if s.notes:
        parts.append("Примечания: " + "; ".join(s.notes))
    return "\n".join(parts) + "\n(Описание листа сделано ИИ по изображению.)"

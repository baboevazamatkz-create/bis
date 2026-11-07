"""Раскладка таблиц и штампов: ИИ задаёт содержимое, геометрию и подгонку текста считает код.

Почему так: модель не знает ширину надписи и подбирает координаты «на глаз» — отсюда наезды текста на линии
и друг на друга. Здесь ширина текста оценивается (а в AutoCAD/nanoCAD потом уточняется по габаритам),
длинный текст переносится на вторую строку или сужается, всё выравнивается по центру ячейки по высоте.

Размеры — в мм листа; `scale` переводит их в мм модели (масштаб 1:100 → scale=100).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHAR = 0.85  # средняя ширина символа в долях высоты прописной (кириллица: Arial, GOST type A, txt.shx)
PAD = 1.0  # отступ текста от линий ячейки, мм листа
MIN_WIDTH = 0.65  # сильнее сжимать нечитаемо — тогда уменьшаем высоту


def text_width(s: str, height: float, width_factor: float = 1.0) -> float:
    narrow = sum(1 for c in s if c in " .,:;!|'")
    return (len(s) - narrow * 0.5) * height * CHAR * width_factor


@dataclass
class Text:
    s: str
    x: float
    y: float  # точка вставки: левый край (или центр при align=center) и середина строки по высоте
    height: float
    width_factor: float = 1.0
    align: str = "left"  # left | center
    max_width: float | None = None


@dataclass
class Layout:
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)


def fit(s: str, width: float, height: float, text_h: float) -> tuple[list[str], float, float]:
    """Как уместить текст в ячейку: (строки, высота, сжатие)."""
    s = " ".join(str(s).split())
    avail = max(width - 2 * PAD, 1.0) * 0.94  # запас на неточность оценки ширины
    if not s:
        return [], text_h, 1.0
    h = min(text_h, height * 0.6)
    lines = [s]
    for n in (2, 3):  # перенос на 2–3 строки, если ячейка позволяет по высоте
        if text_width(" ".join(lines), h) / len(lines) <= avail and max(text_width(ln, h) for ln in lines) <= avail:
            break
        if height < h * n * 1.45 or s.count(" ") < n - 1:
            break
        lines = _split(s, n, h)
    widest = max(text_width(ln, h) for ln in lines)
    wf = 1.0
    if widest > avail:
        wf = max(MIN_WIDTH, avail / widest)
        if widest * wf > avail:  # даже сжатый не влезает — уменьшаем высоту
            h *= avail / (widest * wf)
    return lines, h, round(wf, 3)


def _split(s: str, n: int, h: float) -> list[str]:
    """Разбить по словам на n строк возможно ровнее."""
    words = s.split(" ")
    target = text_width(s, h) / n
    out, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and text_width(cand, h) > target * 1.08 and len(out) < n - 1:
            out.append(cur)
            cur = w
        else:
            cur = cand
    return out + [cur]


def cell_text(lay: Layout, s: str, x0: float, y0: float, x1: float, y1: float, text_h: float, align: str = "left") -> None:
    """Текст в прямоугольнике (x0,y0)-(x1,y1): по центру по высоте, слева или по центру по ширине."""
    lines, h, wf = fit(s, x1 - x0, y1 - y0, text_h)
    if not lines:
        return
    gap = h * 1.45
    top = (y0 + y1) / 2 + gap * (len(lines) - 1) / 2
    for i, ln in enumerate(lines):
        x = (x0 + x1) / 2 if align == "center" else x0 + PAD
        lay.texts.append(Text(ln, x, top - i * gap, h, wf, align, x1 - x0 - 2 * PAD))


def table(x: float, y: float, columns: list[float], rows: list[list[Any]], row_heights: list[float] | float = 8.0,
          text_h: float = 2.5, header: bool = False) -> Layout:
    """Таблица от левого верхнего угла (x, y) вниз. Ячейка — строка или {text, span, align}."""
    lay = Layout()
    heights = row_heights if isinstance(row_heights, list) else [row_heights] * len(rows)
    width = sum(columns)
    edges = [x]
    for w in columns:
        edges.append(edges[-1] + w)
    cy = y
    lay.lines.append(((x, y), (x + width, y)))
    for r, row in enumerate(rows):
        h = heights[r] if r < len(heights) else heights[-1]
        col = 0
        lay.lines.append(((x, cy), (x, cy - h)))
        for cell in row:
            if col >= len(columns):
                break
            c = cell if isinstance(cell, dict) else {"text": cell}
            span = max(1, int(c.get("span", 1)))
            x0, x1 = edges[col], edges[min(col + span, len(columns))]
            align = c.get("align") or ("center" if header and r == 0 else "left")
            cell_text(lay, c.get("text", "") or "", x0, cy - h, x1, cy, float(c.get("height", text_h)), align)
            lay.lines.append(((x1, cy), (x1, cy - h)))
            col += span
        if col < len(columns):  # недостающие ячейки — пустые
            for k in range(col + 1, len(columns) + 1):
                lay.lines.append(((edges[k], cy), (edges[k], cy - h)))
        cy -= h
        lay.lines.append(((x, cy), (x + width, cy)))
    return lay


def title_block(x: float, y: float, f: dict[str, Any]) -> Layout:
    """Основная надпись по форме 3 ГОСТ Р 21.101 (185×55 мм) — правый нижний угол в (x, y).

    f: code, object, title, stage, sheet, sheets, organization, signatures:[{role, name, date}] (до 5)."""
    x0, y0 = x - 185, y
    lay = Layout()

    def box(ax, ay, bx, by):
        lay.lines.extend([((ax, ay), (bx, ay)), ((bx, ay), (bx, by)), ((bx, by), (ax, by)), ((ax, by), (ax, ay))])

    box(x0, y0, x, y0 + 55)
    # левая часть: 65 мм, строки по 5 мм
    lx = [0, 10, 20, 30, 40, 55, 65]
    for k in range(1, 11):
        yy = y0 + k * 5
        lay.lines.append(((x0, yy), (x0 + 65, yy)))
    lay.lines.append(((x0 + 65, y0), (x0 + 65, y0 + 55)))
    for c in (10, 20, 30, 40, 55):  # графы изменений и заголовок — над подписями
        lay.lines.append(((x0 + c, y0 + 25), (x0 + c, y0 + 55)))
    for c in (20, 40, 55):  # подписи: должность | фамилия | подпись | дата
        lay.lines.append(((x0 + c, y0), (x0 + c, y0 + 25)))
    for i, head in enumerate(["Изм.", "Кол.уч", "Лист", "№ док.", "Подп.", "Дата"]):
        cell_text(lay, head, x0 + lx[i], y0 + 25, x0 + lx[i + 1], y0 + 30, 2.5, "center")
    sigs = list(f.get("signatures") or [])[:5]
    for i, s in enumerate(sigs):
        top = y0 + 25 - i * 5
        cell_text(lay, s.get("role", ""), x0, top - 5, x0 + 20, top, 2.5)
        cell_text(lay, s.get("name", ""), x0 + 20, top - 5, x0 + 40, top, 2.5)
        cell_text(lay, s.get("date", ""), x0 + 55, top - 5, x0 + 65, top, 2.5, "center")
    # правая часть: 120 мм
    rx = x0 + 65
    lay.lines.append(((rx, y0 + 40), (x, y0 + 40)))
    lay.lines.append(((rx, y0 + 25), (x, y0 + 25)))
    lay.lines.append(((rx + 70, y0), (rx + 70, y0 + 25)))
    lay.lines.append(((rx + 70, y0 + 20), (x, y0 + 20)))
    lay.lines.append(((rx + 70, y0 + 15), (x, y0 + 15)))
    lay.lines.append(((rx + 85, y0 + 15), (rx + 85, y0 + 25)))
    lay.lines.append(((rx + 100, y0 + 15), (rx + 100, y0 + 25)))
    cell_text(lay, f.get("code", ""), rx, y0 + 40, x, y0 + 55, 5, "center")
    cell_text(lay, f.get("object", ""), rx, y0 + 25, x, y0 + 40, 3.5, "center")
    cell_text(lay, f.get("title", ""), rx, y0, rx + 70, y0 + 25, 3.5, "center")
    for (a, b), head, key in zip([(70, 85), (85, 100), (100, 120)], ["Стадия", "Лист", "Листов"], ["stage", "sheet", "sheets"], strict=True):
        cell_text(lay, head, rx + a, y0 + 20, rx + b, y0 + 25, 2.5, "center")
        cell_text(lay, str(f.get(key, "") or ""), rx + a, y0 + 15, rx + b, y0 + 20, 2.5, "center")
    cell_text(lay, f.get("organization", ""), rx + 70, y0, x, y0 + 15, 3.5, "center")
    return lay


def scaled(lay: Layout, scale: float, origin: tuple[float, float]) -> Layout:
    """Из мм листа в мм модели: всё масштабируется от точки привязки."""
    if scale == 1:
        return lay
    ox, oy = origin

    def p(pt):
        return (ox + (pt[0] - ox) * scale, oy + (pt[1] - oy) * scale)

    out = Layout(lines=[(p(a), p(b)) for a, b in lay.lines])
    for t in lay.texts:
        x, y = p((t.x, t.y))
        out.texts.append(Text(t.s, x, y, t.height * scale, t.width_factor, t.align,
                              t.max_width * scale if t.max_width else None))
    return out


def overlaps(boxes: list[tuple[str, float, float, float, float]], tol: float = 0.05) -> list[str]:
    """Пересекающиеся надписи: [(текст, xmin, ymin, xmax, ymax)] → описания наложений."""
    out = []
    for i in range(len(boxes)):
        a = boxes[i]
        for b in boxes[i + 1:]:
            w = min(a[3], b[3]) - max(a[1], b[1])
            h = min(a[4], b[4]) - max(a[2], b[2])
            small = min(a[4] - a[2], b[4] - b[2]) or 1
            if w > tol * small and h > tol * small:
                out.append(f"«{a[0][:40]}» наезжает на «{b[0][:40]}»")
    return out

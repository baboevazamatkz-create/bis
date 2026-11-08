"""Две колонки в тексте: «Заказчик … Подрядчик …» в реквизитах, подписи сторон, таблицы-штампы.

Текстовый слой PDF и распознавание отдают такие места построчно, вперемешку: слева ИНН заказчика, справа ИНН
подрядчика — в одной строке. Модель потом путает стороны. Здесь такой блок раскладывается: сначала вся левая
колонка, затем вся правая, — и каждая сторона читается целиком.
"""

from __future__ import annotations

import re
from statistics import median

GAP = re.compile(r"\S( {3,}|\t+)\S")
MIN_LINES = 3  # меньше трёх строк с разрывом — это не колонки, а выравнивание


def _split_at(line: str) -> int | None:
    m = GAP.search(line)
    return m.start(1) if m else None


def split_columns(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _split_at(lines[i]) is None:
            out.append(lines[i])
            i += 1
            continue
        # блок: строки с разрывом, между ними допускается одна строка без разрыва (перенос в колонке)
        j, cols, last, single = i, [], i, 0
        while j < len(lines) and j - last <= 2 and lines[j].strip():
            pos = _split_at(lines[j])
            if pos is not None:
                cols.append(pos)
                single += len(GAP.findall(lines[j])) == 1
                last = j
            j += 1
        block = lines[i:last + 1]
        # таблица (смета, ведомость): в строках несколько разрывов — строки не разрываем, иначе «бетон» уйдёт от «10 м3»
        if len(cols) < MIN_LINES or single < 0.7 * len(cols):
            out.extend(block)
            i = last + 1
            continue
        mid = median(cols)
        left, right = [], []
        for ln in block:
            # разрыв ближе всего к середине блока — граница колонок; строки без разрыва — продолжение левой
            gaps = [m.start(1) for m in GAP.finditer(ln)]
            if not gaps:
                left.append(ln.strip())
                continue
            g = min(gaps, key=lambda p: abs(p - mid))
            left.append(ln[:g].strip())
            right.append(ln[g:].strip())
        out.extend([x for x in left if x] + [""] + [x for x in right if x])
        i = last + 1
    return "\n".join(out)

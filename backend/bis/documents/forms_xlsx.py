"""Табличные формы в XLSX: КС-2, КС-3 (унифицированные формы, пост. Госкомстата России от 11.11.1999 № 100),
отчёт о расходе материалов (М-29), акт списания материалов."""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

from bis.documents.common import fmt_date

THIN = Side(style="thin", color="000000")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
MONEY = '#,##0.00'
QTY = '#,##0.0###'


class SheetBuilder:
    def __init__(self, title: str, widths: Sequence[float]):
        self.wb = Workbook()
        self.ws = self.wb.active
        self.ws.title = title[:31]
        self.cols = len(widths)
        for i, w in enumerate(widths, 1):
            self.ws.column_dimensions[get_column_letter(i)].width = w
        self.ws.page_setup.orientation = "landscape"
        self.ws.page_setup.fitToWidth = 1
        self.ws.sheet_properties.pageSetUpPr.fitToPage = True
        self.row = 1

    def line(self, text: str = "", *, bold=False, size=10, align="left", italic=False, merge=True) -> None:
        c = self.ws.cell(self.row, 1, text)
        c.font = Font(bold=bold, size=size, italic=italic)
        c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=True)
        if merge and self.cols > 1:
            self.ws.merge_cells(start_row=self.row, start_column=1, end_row=self.row, end_column=self.cols)
        self.row += 1

    def pair(self, label: str, value: str) -> None:
        """«Метка: значение» в одной объединённой строке; значение подчёркнуто."""
        c = self.ws.cell(self.row, 1)
        c.value = CellRichText(TextBlock(InlineFont(sz=9), f"{label}: "),
                               TextBlock(InlineFont(sz=10, u="single"), value or "—"))
        c.alignment = Alignment(wrap_text=True, vertical="center")
        if self.cols > 1:
            self.ws.merge_cells(start_row=self.row, start_column=1, end_row=self.row, end_column=self.cols)
        self.row += 1

    def header(self, titles: Sequence[str]) -> None:
        for i, t in enumerate(titles, 1):
            c = self.ws.cell(self.row, i, t)
            c.font, c.alignment, c.border = Font(bold=True, size=9), CENTER, BORDER
        self.ws.row_dimensions[self.row].height = 45
        self.row += 1
        for i in range(1, len(titles) + 1):
            c = self.ws.cell(self.row, i, i)
            c.font, c.alignment, c.border = Font(size=8), CENTER, BORDER
        self.row += 1

    def data(self, values: Sequence[Any], formats: Sequence[str | None] | None = None, *, bold=False) -> None:
        for i, v in enumerate(values, 1):
            if isinstance(v, Decimal):
                v = float(v)
            c = self.ws.cell(self.row, i, v)
            c.border, c.alignment = BORDER, WRAP
            c.font = Font(size=9, bold=bold)
            if formats and i <= len(formats) and formats[i - 1]:
                c.number_format = formats[i - 1]
        self.row += 1

    def total(self, label: str, value: Decimal | float | None, label_col: int, value_col: int) -> None:
        c = self.ws.cell(self.row, label_col, label)
        c.font, c.alignment = Font(bold=True, size=9), Alignment(horizontal="right")
        if value_col - label_col > 1:
            self.ws.merge_cells(start_row=self.row, start_column=label_col, end_row=self.row, end_column=value_col - 1)
        v = self.ws.cell(self.row, value_col, float(value) if value is not None else None)
        v.font, v.number_format, v.border = Font(bold=True, size=9), MONEY, BORDER
        self.row += 1

    def signatures(self, pairs: Sequence[tuple[str, str]]) -> None:
        self.row += 1
        for role, name in pairs:
            self.ws.cell(self.row, 1, role).font = Font(size=9, bold=True)
            self.ws.cell(self.row, 3, "_________________ (подпись)").font = Font(size=9)
            self.ws.cell(self.row, max(5, self.cols - 2), name).font = Font(size=9)
            self.row += 2

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        self.wb.save(buf)
        return buf.getvalue()


def render_ks2(*, number: str, date: dt.date, period: tuple[dt.date, dt.date], investor: str | None, customer: str,
               contractor: str, construction: str, obj: str, contract: str, estimate_total: Decimal | None,
               rows: list[dict], total: Decimal, vat_rate: Decimal, vat: Decimal, signers: tuple[str, str]) -> bytes:
    s = SheetBuilder("КС-2", [7, 10, 56, 14, 12, 12, 14, 16])
    s.line("Унифицированная форма № КС-2. Утверждена постановлением Госкомстата России от 11.11.99 № 100",
           size=8, align="right", italic=True)
    s.pair("Инвестор", investor or "")
    s.pair("Заказчик (генподрядчик)", customer)
    s.pair("Подрядчик (субподрядчик)", contractor)
    s.pair("Стройка", construction)
    s.pair("Объект", obj)
    s.pair("Договор подряда (контракт)", contract)
    s.row += 1
    s.line("АКТ О ПРИЁМКЕ ВЫПОЛНЕННЫХ РАБОТ", bold=True, size=13, align="center")
    s.line(f"№ {number} от {fmt_date(date)}   Отчётный период: с {fmt_date(period[0])} по {fmt_date(period[1])}", align="center")
    if estimate_total is not None:
        s.line(f"Сметная (договорная) стоимость в соответствии с договором подряда: {estimate_total:,.2f} руб.".replace(",", " "))
    s.row += 1
    s.header(["№ по порядку", "№ позиции по смете", "Наименование работ", "Номер единичной расценки", "Единица измерения",
              "Количество", "Цена за единицу, руб.", "Стоимость, руб."])
    for i, r in enumerate(rows, 1):
        s.data([i, r["ord"], r["name"], r["code"] or "", r["unit"], r["include_quantity"], r["unit_price"], r["amount"]],
               [None, None, None, None, None, QTY, MONEY, MONEY])
    s.total("Итого", total, 3, 8)
    s.total(f"НДС {vat_rate}%", vat, 3, 8)
    s.total("Всего с НДС", total + vat, 3, 8)
    s.signatures([("Сдал", signers[0]), ("Принял", signers[1])])
    return s.to_bytes()


def render_ks3(*, number: str, date: dt.date, period: tuple[dt.date, dt.date], investor: str | None, customer: str,
               contractor: str, construction: str, obj: str, contract: str, totals: dict[str, Decimal],
               vat_rate: Decimal, signers: tuple[str, str]) -> bytes:
    s = SheetBuilder("КС-3", [6, 60, 10, 20, 20, 20])
    s.line("Унифицированная форма № КС-3. Утверждена постановлением Госкомстата России от 11.11.99 № 100",
           size=8, align="right", italic=True)
    s.pair("Инвестор", investor or "")
    s.pair("Заказчик (генподрядчик)", customer)
    s.pair("Подрядчик (субподрядчик)", contractor)
    s.pair("Стройка", construction)
    s.pair("Договор подряда (контракт)", contract)
    s.row += 1
    s.line("СПРАВКА О СТОИМОСТИ ВЫПОЛНЕННЫХ РАБОТ И ЗАТРАТ", bold=True, size=13, align="center")
    s.line(f"№ {number} от {fmt_date(date)}   Отчётный период: с {fmt_date(period[0])} по {fmt_date(period[1])}", align="center")
    s.row += 1
    s.header(["№ по порядку", "Наименование пусковых комплексов, этапов, объектов, видов выполненных работ, оборудования, затрат",
              "Код", "Стоимость с начала проведения работ, руб.", "Стоимость с начала года, руб.",
              "в том числе за отчётный период, руб."])

    def vat(x: Decimal) -> Decimal:
        return (x * vat_rate / 100).quantize(Decimal("0.01"))

    t = totals
    s.data([1, "Всего работ и затрат, включаемых в стоимость работ", "", t["since_start"], t["since_year"], t["period"]],
           [None, None, None, MONEY, MONEY, MONEY])
    s.data(["", f"в том числе: {obj}", "", t["since_start"], t["since_year"], t["period"]], [None, None, None, MONEY, MONEY, MONEY])
    s.data(["", "Итого", "", t["since_start"], t["since_year"], t["period"]], [None, None, None, MONEY, MONEY, MONEY], bold=True)
    s.data(["", f"Сумма НДС {vat_rate}%", "", vat(t["since_start"]), vat(t["since_year"]), vat(t["period"])],
           [None, None, None, MONEY, MONEY, MONEY])
    s.data(["", "Всего с учётом НДС", "", t["since_start"] + vat(t["since_start"]), t["since_year"] + vat(t["since_year"]),
            t["period"] + vat(t["period"])], [None, None, None, MONEY, MONEY, MONEY], bold=True)
    s.signatures([("Заказчик (генподрядчик)", signers[1]), ("Подрядчик (субподрядчик)", signers[0])])
    return s.to_bytes()


def render_m29(*, org: str, obj: str, period: tuple[dt.date, dt.date], rows: list[dict], signer: str) -> bytes:
    s = SheetBuilder("М-29", [5, 34, 8, 12, 44, 12, 12, 12, 13, 13, 13])
    s.line("Форма М-29", size=8, align="right", italic=True)
    s.line("ОТЧЁТ О РАСХОДЕ ОСНОВНЫХ МАТЕРИАЛОВ В СТРОИТЕЛЬСТВЕ В СОПОСТАВЛЕНИИ С РАСХОДОМ, ОПРЕДЕЛЁННЫМ ПО "
           "ПРОИЗВОДСТВЕННЫМ НОРМАМ", bold=True, size=11, align="center")
    s.pair("Организация", org)
    s.pair("Объект", obj)
    s.pair("Отчётный период", f"с {fmt_date(period[0])} по {fmt_date(period[1])}")
    s.row += 1
    s.header(["№", "Материал", "Ед. изм.", "№ позиции / шифр", "Наименование работ", "Объём работ", "Норма на ед.",
              "Потери, %", "Расход по норме", "Фактический расход", "Отклонение (+ перерасход / − экономия)"])
    for i, r in enumerate(rows, 1):
        lines = r["lines"] or [None]
        for j, ln in enumerate(lines):
            first = j == 0
            s.data([i if first else "", r["material"] if first else "", r["unit"] if first else "",
                    ln["item_code"] or "" if ln else "", ln["item_name"] if ln else "нет нормы",
                    ln["work_quantity"] if ln else None, ln["rate"] if ln else None, ln["loss_percent"] if ln else None,
                    ln["norm_quantity"] if ln else None, None, None],
                   [None, None, None, None, None, QTY, '0.0#####', '0.##', QTY, QTY, QTY])
        s.data(["", f"Итого: {r['material']}", r["unit"], "", "", None, None, None, r["norm_total"], r["actual"],
                r["deviation"]], [None, None, None, None, None, None, None, None, QTY, QTY, QTY], bold=True)
    s.signatures([("Начальник участка (прораб)", signer), ("Проверил (ПТО)", "")])
    return s.to_bytes()


def render_writeoff(*, number: str, date: dt.date, org: str, obj: str, period: tuple[dt.date, dt.date], rows: list[dict],
                    total: Decimal, commission: Sequence[str]) -> bytes:
    s = SheetBuilder("Акт списания", [5, 40, 8, 13, 13, 13, 13, 13, 15])
    s.line(org, bold=True, align="center")
    s.line(f"АКТ № {number} от {fmt_date(date)}", bold=True, size=12, align="center")
    s.line("на списание материалов, израсходованных при производстве работ", align="center")
    s.pair("Объект", obj)
    s.pair("Период", f"с {fmt_date(period[0])} по {fmt_date(period[1])}")
    s.line("Основание: отчёт о расходе материалов (форма М-29) за период; расход по производственным нормам.", size=9)
    s.row += 1
    s.header(["№", "Материал", "Ед. изм.", "Расход по норме", "Фактический расход", "Списать", "Сверх нормы (не списано)",
              "Цена, руб.", "Сумма, руб."])
    for i, r in enumerate(rows, 1):
        s.data([i, r["material"], r["unit"], r["norm"], r["actual"], r["write_off"], r["excess"], r["price"], r["amount"]],
               [None, None, None, QTY, QTY, QTY, QTY, MONEY, MONEY])
    s.total("Итого списано на сумму", total, 2, 9)
    s.line("Материалы сверх нормы списываются только после служебной проверки и обоснования перерасхода.", size=8, italic=True)
    s.signatures([(f"Член комиссии: {m}", "") for m in commission] or [("Председатель комиссии", "")])
    return s.to_bytes()

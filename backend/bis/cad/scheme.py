"""Исполнительная геодезическая схема в DXF (открывается в AutoCAD и nanoCAD).

По проектным и фактическим координатам точек строится схема: оси, контур конструкции, точки,
отклонения в мм по осям (стрелка в сторону смещения), высотные отметки факт/проект, проверка допусков,
таблица отклонений и штамп. Оформление — по принципам ГОСТ Р 51872-2019 (исполнительная геодезическая
документация); отклонения сверх допуска выделяются красным.

Единицы чертежа — миллиметры (1:1 в пространстве модели); масштаб листа подбирается автоматически.
"""

from __future__ import annotations

import csv
import io
import math
import re
from decimal import Decimal

import ezdxf
from ezdxf.enums import TextEntityAlignment
from pydantic import BaseModel, Field

STANDARD_SCALES = [20, 50, 100, 200, 250, 500, 1000, 2000]
A3 = (420.0, 297.0)
STAMP = (185.0, 55.0)
FONT = "arial.ttf"  # кириллица в AutoCAD/nanoCAD


class Axis(BaseModel):
    name: str
    coord: float = Field(description="Координата оси, м (для поперечных осей — X, для продольных — Y)")


class SurveyPoint(BaseModel):
    name: str
    design_x: float
    design_y: float
    fact_x: float | None = None
    fact_y: float | None = None
    design_z: float | None = None
    fact_z: float | None = None


class SchemeData(BaseModel):
    title: str = Field(description="Наименование схемы")
    object_name: str = ""
    organization: str = ""
    code: str = Field("", description="Шифр (например, ЖК-01-ИГС-01)")
    surveyor: str = Field("", description="Исполнитель (геодезист)")
    checker: str = Field("", description="Проверил (ПТО / стройконтроль)")
    date: str = ""
    x_axes: list[Axis] = Field(default_factory=list, description="Поперечные оси (1, 2, 3 …) — вертикальные линии")
    y_axes: list[Axis] = Field(default_factory=list, description="Продольные оси (А, Б, В …) — горизонтальные линии")
    contour: list[tuple[float, float]] = Field(default_factory=list, description="Проектный контур конструкции, м")
    points: list[SurveyPoint]
    tolerance_plan_mm: float = 15
    tolerance_height_mm: float = 10
    notes: list[str] = Field(default_factory=list)


class PointCheck(BaseModel):
    name: str
    dx_mm: int | None
    dy_mm: int | None
    dz_mm: int | None
    plan_ok: bool | None
    height_ok: bool | None


def check_points(d: SchemeData) -> list[PointCheck]:
    out = []
    for p in d.points:
        dx = round((p.fact_x - p.design_x) * 1000) if p.fact_x is not None else None
        dy = round((p.fact_y - p.design_y) * 1000) if p.fact_y is not None else None
        dz = round((p.fact_z - p.design_z) * 1000) if p.fact_z is not None and p.design_z is not None else None
        plan_ok = None if dx is None and dy is None else max(abs(dx or 0), abs(dy or 0)) <= d.tolerance_plan_mm
        height_ok = None if dz is None else abs(dz) <= d.tolerance_height_mm
        out.append(PointCheck(name=p.name, dx_mm=dx, dy_mm=dy, dz_mm=dz, plan_ok=plan_ok, height_ok=height_ok))
    return out


def summary(checks: list[PointCheck], d: SchemeData) -> dict:
    bad = [c for c in checks if c.plan_ok is False or c.height_ok is False]
    return {
        "points": len(checks),
        "out_of_tolerance": [c.name for c in bad],
        "max_plan_mm": max((max(abs(c.dx_mm or 0), abs(c.dy_mm or 0)) for c in checks), default=0),
        "max_height_mm": max((abs(c.dz_mm) for c in checks if c.dz_mm is not None), default=None),
        "tolerance_plan_mm": d.tolerance_plan_mm,
        "tolerance_height_mm": d.tolerance_height_mm,
        "measured": sum(1 for c in checks if c.plan_ok is not None or c.height_ok is not None),
        "conclusion": _conclusion(checks, bad),
    }


def _conclusion(checks: list[PointCheck], bad: list[PointCheck]) -> str:
    measured = [c for c in checks if c.plan_ok is not None or c.height_ok is not None]
    if not measured:
        return "Фактические замеры не внесены — схема по проекту, графы факта заполняются по замерам"
    head = "" if len(measured) == len(checks) else f"Замерено {len(measured)} из {len(checks)} точек. "
    if bad:
        return head + f"Превышение допусков в точках: {', '.join(c.name for c in bad)}"
    return head + "Отклонения в пределах допусков"


def _pick_scale(width_m: float, height_m: float) -> int:
    avail_w, avail_h = A3[0] - 30, A3[1] - 20 - STAMP[1] - 15
    for s in STANDARD_SCALES:
        if width_m * 1000 / s <= avail_w and height_m * 1000 / s <= avail_h:
            return s
    return STANDARD_SCALES[-1]


def _sign(v: int) -> str:
    return f"+{v}" if v > 0 else str(v)


def _fmt(v: int | None) -> str:
    return "—" if v is None else _sign(v)


def build_dxf(d: SchemeData) -> tuple[ezdxf.document.Drawing, list[PointCheck], int]:
    checks = check_points(d)
    xs = [p.design_x for p in d.points] + [a.coord for a in d.x_axes] + [c[0] for c in d.contour]
    ys = [p.design_y for p in d.points] + [a.coord for a in d.y_axes] + [c[1] for c in d.contour]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    margin = max(maxx - minx, maxy - miny) * 0.15 + 1.0
    minx, maxx, miny, maxy = minx - margin, maxx + margin, miny - margin, maxy + margin
    scale = _pick_scale(maxx - minx, maxy - miny)
    k = scale  # 1 мм на листе = k мм в модели
    th = 2.5 * k  # высота текста 2,5 мм на листе

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    doc.styles.new("BIS", dxfattribs={"font": FONT})
    for name, color, lt in [("BIS_AXES", 8, "DASHDOT"), ("BIS_CONTOUR", 7, "CONTINUOUS"), ("BIS_POINTS", 5, "CONTINUOUS"),
                            ("BIS_DEV_OK", 94, "CONTINUOUS"), ("BIS_DEV_BAD", 1, "CONTINUOUS"), ("BIS_TEXT", 7, "CONTINUOUS"),
                            ("BIS_FRAME", 7, "CONTINUOUS")]:
        doc.layers.add(name, color=color, linetype=lt)
    msp = doc.modelspace()

    def mm(x: float, y: float) -> tuple[float, float]:
        return (x * 1000, y * 1000)

    def text(s: str, pos, h=th, layer="BIS_TEXT", align=TextEntityAlignment.LEFT, color=None):
        attrs = {"layer": layer, "style": "BIS"}
        if color is not None:
            attrs["color"] = color
        msp.add_text(s, height=h, dxfattribs=attrs).set_placement(pos, align=align)

    # оси с марками
    bubble = 4 * k
    for a in d.x_axes:
        x = a.coord * 1000
        msp.add_line((x, miny * 1000 + bubble * 2), (x, maxy * 1000 - bubble * 2), dxfattribs={"layer": "BIS_AXES"})
        for yy in (miny * 1000 + bubble, maxy * 1000 - bubble):
            msp.add_circle((x, yy), bubble, dxfattribs={"layer": "BIS_AXES"})
            text(a.name, (x, yy), h=th * 1.2, layer="BIS_AXES", align=TextEntityAlignment.MIDDLE_CENTER)
    for a in d.y_axes:
        y = a.coord * 1000
        msp.add_line((minx * 1000 + bubble * 2, y), (maxx * 1000 - bubble * 2, y), dxfattribs={"layer": "BIS_AXES"})
        for xx in (minx * 1000 + bubble, maxx * 1000 - bubble):
            msp.add_circle((xx, y), bubble, dxfattribs={"layer": "BIS_AXES"})
            text(a.name, (xx, y), h=th * 1.2, layer="BIS_AXES", align=TextEntityAlignment.MIDDLE_CENTER)
    if len(d.contour) >= 3:
        msp.add_lwpolyline([mm(*c) for c in d.contour], close=True, dxfattribs={"layer": "BIS_CONTOUR", "lineweight": 50})

    # точки и отклонения
    arrow = 6 * k
    for p, c in zip(d.points, checks, strict=True):
        px, py = mm(p.design_x, p.design_y)
        msp.add_circle((px, py), 0.8 * k, dxfattribs={"layer": "BIS_POINTS"})
        text(p.name, (px - 1.5 * k, py + 1.5 * k), h=th * 0.9, layer="BIS_POINTS", align=TextEntityAlignment.BOTTOM_RIGHT)
        plan_layer = "BIS_DEV_OK" if c.plan_ok is not False else "BIS_DEV_BAD"
        if c.dx_mm:
            sx = math.copysign(1, c.dx_mm)
            msp.add_line((px, py), (px + sx * arrow, py), dxfattribs={"layer": plan_layer})
            msp.add_solid([(px + sx * arrow, py), (px + sx * (arrow - 1.5 * k), py + 0.6 * k),
                           (px + sx * (arrow - 1.5 * k), py - 0.6 * k)], dxfattribs={"layer": plan_layer})
            text(_sign(c.dx_mm), (px + sx * (arrow + 1 * k), py),
                 layer=plan_layer, align=TextEntityAlignment.MIDDLE_LEFT if sx > 0 else TextEntityAlignment.MIDDLE_RIGHT)
        if c.dy_mm:
            sy = math.copysign(1, c.dy_mm)
            msp.add_line((px, py), (px, py + sy * arrow), dxfattribs={"layer": plan_layer})
            msp.add_solid([(px, py + sy * arrow), (px + 0.6 * k, py + sy * (arrow - 1.5 * k)),
                           (px - 0.6 * k, py + sy * (arrow - 1.5 * k))], dxfattribs={"layer": plan_layer})
            text(_sign(c.dy_mm), (px, py + sy * (arrow + 1 * k)),
                 layer=plan_layer, align=TextEntityAlignment.BOTTOM_CENTER if sy > 0 else TextEntityAlignment.TOP_CENTER)
        if p.fact_z is not None and p.design_z is not None:
            h_layer = "BIS_DEV_OK" if c.height_ok else "BIS_DEV_BAD"
            # отметки — в четверти, свободной от стрелки смещения по X
            right = (c.dx_mm or 0) < 0
            hx = px + 2 * k if right else px - 2 * k
            al_top = TextEntityAlignment.TOP_LEFT if right else TextEntityAlignment.TOP_RIGHT
            al_bot = TextEntityAlignment.BOTTOM_LEFT if right else TextEntityAlignment.BOTTOM_RIGHT
            yline = py - 2.5 * k - th
            text(f"{p.fact_z:.3f}", (hx, yline + 0.4 * k), h=th * 0.9, layer=h_layer, align=al_bot)
            msp.add_line((hx, yline), (hx + (1 if right else -1) * 6 * th * 0.9, yline), dxfattribs={"layer": h_layer})
            text(f"{p.design_z:.3f}", (hx, yline - 0.4 * k), h=th * 0.9, layer=h_layer, align=al_top)

    # рамка листа A3 и штамп
    fx0, fy0 = minx * 1000 - 20 * k, miny * 1000 - (STAMP[1] + 15) * k
    fw, fh = A3[0] * k, A3[1] * k
    content_w = (maxx - minx) * 1000
    if content_w + 40 * k > fw:
        fw = content_w + 40 * k
    msp.add_lwpolyline([(fx0, fy0), (fx0 + fw, fy0), (fx0 + fw, fy0 + fh), (fx0, fy0 + fh)], close=True,
                       dxfattribs={"layer": "BIS_FRAME"})
    msp.add_lwpolyline([(fx0 + 20 * k, fy0 + 5 * k), (fx0 + fw - 5 * k, fy0 + 5 * k), (fx0 + fw - 5 * k, fy0 + fh - 5 * k),
                        (fx0 + 20 * k, fy0 + fh - 5 * k)], close=True, dxfattribs={"layer": "BIS_FRAME", "lineweight": 50})
    sx0, sy0 = fx0 + fw - 5 * k - STAMP[0] * k, fy0 + 5 * k
    sw, sh = STAMP[0] * k, STAMP[1] * k
    msp.add_lwpolyline([(sx0, sy0), (sx0 + sw, sy0), (sx0 + sw, sy0 + sh), (sx0, sy0 + sh)], close=True,
                       dxfattribs={"layer": "BIS_FRAME", "lineweight": 50})
    for frac in (0.25, 0.5, 0.75):
        msp.add_line((sx0, sy0 + sh * frac), (sx0 + sw, sy0 + sh * frac), dxfattribs={"layer": "BIS_FRAME"})
    msp.add_line((sx0 + sw * 0.45, sy0), (sx0 + sw * 0.45, sy0 + sh * 0.75), dxfattribs={"layer": "BIS_FRAME"})
    pad = 2 * k
    text(d.code or "", (sx0 + pad, sy0 + sh * 0.875), h=th * 1.4, align=TextEntityAlignment.MIDDLE_LEFT)
    text((d.object_name or "")[:90], (sx0 + pad, sy0 + sh * 0.625), align=TextEntityAlignment.MIDDLE_LEFT)
    title_cell = msp.add_mtext(d.title, dxfattribs={"layer": "BIS_TEXT", "style": "BIS", "char_height": th * 0.8,
                                                    "width": sw * 0.55 - 2 * pad})
    title_cell.set_location((sx0 + sw * 0.45 + pad, sy0 + sh * 0.5 - pad / 2), attachment_point=1)
    text(f"М 1:{scale}   Отклонения — в мм", (sx0 + sw * 0.45 + pad, sy0 + sh * 0.125), h=th * 0.9,
         align=TextEntityAlignment.MIDDLE_LEFT)
    text(f"Исполнил: {d.surveyor}", (sx0 + pad, sy0 + sh * 0.375), h=th * 0.9, align=TextEntityAlignment.MIDDLE_LEFT)
    text(f"Проверил: {d.checker}", (sx0 + pad, sy0 + sh * 0.125), h=th * 0.9, align=TextEntityAlignment.MIDDLE_LEFT)
    text(f"{d.organization}   {d.date}"[:80], (sx0 + pad, sy0 + sh + 2 * k), h=th * 0.9)

    # таблица отклонений (до 25 точек; полный перечень — в данных документа)
    if 0 < len(checks) <= 25:
        cw = [12 * k, 16 * k, 16 * k, 16 * k, 22 * k]
        rh = 5 * k
        tx0 = fx0 + fw - 5 * k - sum(cw) - 3 * k
        ty0 = fy0 + fh - 15 * k
        heads = ["Точка", "ΔX, мм", "ΔY, мм", "ΔH, мм", "Допуск"]
        for r_i, row in enumerate([heads] + [[c.name, _fmt(c.dx_mm), _fmt(c.dy_mm), _fmt(c.dz_mm),
                                               "в норме" if c.plan_ok is not False and c.height_ok is not False else "превышен"]
                                              for c in checks]):
            y = ty0 - r_i * rh
            x = tx0
            bad = row[-1] == "превышен"
            for w, val in zip(cw, row, strict=True):
                msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y - rh), (x, y - rh)], close=True,
                                   dxfattribs={"layer": "BIS_FRAME"})
                text(str(val), (x + w / 2, y - rh / 2), h=th * 0.8, layer="BIS_DEV_BAD" if bad else "BIS_TEXT",
                     align=TextEntityAlignment.MIDDLE_CENTER)
                x += w

    # заголовок, условные обозначения, примечания
    tx, ty = fx0 + 25 * k, fy0 + fh - 12 * k
    text(d.title, (tx, ty), h=th * 1.6)
    legend = [f"Допуски: в плане ±{d.tolerance_plan_mm:g} мм, по высоте ±{d.tolerance_height_mm:g} мм.",
              "Стрелка — направление смещения факта от проекта, число — величина, мм.",
              "Отметки: числитель — фактическая, знаменатель — проектная, м.",
              "Красным выделены отклонения сверх допуска.", *d.notes]
    for i, line in enumerate(legend):
        text(line, (sx0, sy0 + sh + 8 * k + i * th * 1.6), h=th * 0.8)
    return doc, checks, scale


def dxf_bytes(doc: ezdxf.document.Drawing) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def svg_preview(doc: ezdxf.document.Drawing) -> str:
    from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg

    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE, lineweight_scaling=0.5)
    backend = svg.SVGBackend()
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(doc.modelspace())
    page = layout.Page(A3[0], A3[1], layout.Units.mm, margins=layout.Margins.all(0))
    return backend.get_string(page, settings=layout.Settings(fit_page=True, output_coordinate_space=100000))


# --- данные съёмки --------------------------------------------------------------------------------

_HEAD = {
    "name": r"точк|пункт|^№|name|имя",
    "design_x": r"x\s*(пр|проект)|проект\w*\s*x|^xп",
    "design_y": r"y\s*(пр|проект)|проект\w*\s*y|^yп",
    "fact_x": r"x\s*(ф|факт)|факт\w*\s*x|^xф",
    "fact_y": r"y\s*(ф|факт)|факт\w*\s*y|^yф",
    "design_z": r"[hz]\s*(пр|проект)|проект\w*\s*(h|z|отм)|^нп|^hп",
    "fact_z": r"[hz]\s*(ф|факт)|факт\w*\s*(h|z|отм)|^нф|^hф",
}


def _f(v) -> float | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(Decimal(str(v).replace(",", ".").replace(" ", "")))
    except ArithmeticError:
        return None


def parse_survey(data: bytes, filename: str, *, geodetic: bool = False) -> list[SurveyPoint]:
    """CSV/TXT (разделитель ; , или таб) или XLSX: Точка, Xпр, Yпр, Xф, Yф, Hпр, Hф.

    geodetic=True — координаты в геодезической системе (X — на север): меняются местами для чертежа.
    """
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        rows = [[c for c in r] for r in ws.iter_rows(values_only=True)]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t") if text.strip() else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    cols: dict[str, int] = {}
    start = 0
    for i, row in enumerate(rows[:10]):
        found = {}
        for j, cell in enumerate(row):
            t = str(cell or "").strip().lower()
            for key, pat in _HEAD.items():
                if key not in found and re.search(pat, t):
                    found[key] = j
                    break
        if {"name", "design_x", "design_y"} <= set(found):
            cols, start = found, i + 1
            break
    if not cols:  # без заголовка: порядок по умолчанию
        cols = {k: i for i, k in enumerate(["name", "design_x", "design_y", "fact_x", "fact_y", "design_z", "fact_z"])}
    points = []
    for row in rows[start:]:
        if not row or all(str(c or "").strip() == "" for c in row):
            continue
        val = {k: (row[j] if j < len(row) else None) for k, j in cols.items()}
        dx, dy = _f(val.get("design_x")), _f(val.get("design_y"))
        if dx is None or dy is None:
            continue
        p = {"name": str(val.get("name") or len(points) + 1).strip(), "design_x": dx, "design_y": dy,
             "fact_x": _f(val.get("fact_x")), "fact_y": _f(val.get("fact_y")),
             "design_z": _f(val.get("design_z")), "fact_z": _f(val.get("fact_z"))}
        if geodetic:
            p["design_x"], p["design_y"] = p["design_y"], p["design_x"]
            p["fact_x"], p["fact_y"] = p["fact_y"], p["fact_x"]
        points.append(SurveyPoint(**p))
    if not points:
        raise ValueError("В файле съёмки не найдено точек (нужны столбцы: точка, X и Y проектные)")
    return points

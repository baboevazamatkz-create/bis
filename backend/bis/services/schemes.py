"""Исполнительные схемы: DXF в реестре документов, SVG-предпросмотр, опционально DWG через ODA File Converter."""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bis.cad.scheme import SchemeData, build_dxf, dxf_bytes, summary, svg_preview
from bis.config import get_settings
from bis.models import ConstructionObject, Document, Organization
from bis.services.documents import save_document
from bis.services.storage import Storage

log = logging.getLogger(__name__)


def convert_to_dwg(dxf: bytes) -> bytes | None:
    """DXF → DWG (AutoCAD 2018) через ODA File Converter, если он установлен (BIS_ODA_CONVERTER)."""
    exe = get_settings().oda_converter
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        Path(src, "scheme.dxf").write_bytes(dxf)
        try:
            subprocess.run([exe, src, dst, "ACAD2018", "DWG", "0", "1", "*.DXF"], check=True, timeout=120,
                           capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("ODA File Converter: %s", e)
            return None
        out = Path(dst, "scheme.dwg")
        return out.read_bytes() if out.exists() else None


def create_scheme(db: Session, storage: Storage, org_id: uuid.UUID, data: SchemeData, *, actor: str,
                  object_id: uuid.UUID | None = None) -> Document:
    doc_dxf, checks, scale = build_dxf(data)
    content = dxf_bytes(doc_dxf)
    info = summary(checks, data)
    preview_key = storage.put(svg_preview(doc_dxf).encode("utf-8"), "preview.svg", prefix=f"documents/{org_id}/previews")
    extra: dict = {"scale": scale, "summary": info, "checks": [c.model_dump() for c in checks],
                   "input": data.model_dump(mode="json"), "preview_key": preview_key}
    dwg = convert_to_dwg(content)
    if dwg:
        extra["dwg_key"] = storage.put(dwg, "scheme.dwg", prefix=f"documents/{org_id}")
    issues = [{"level": "warning", "field": "tolerance", "message": info["conclusion"]}] if info["out_of_tolerance"] else []
    safe = (data.code or "schema").replace("/", "-")
    return save_document(db, storage, org_id=org_id, kind="scheme", title=data.title[:900], data=extra, file_bytes=content,
                         file_name=f"{safe}.dxf", created_by=actor, number=data.code or None, object_id=object_id,
                         issues=issues, mime="image/vnd.dxf")


# --- схема без геодезической съёмки: проект + норматив + замеры прораба ---------------------------

class PointMeasure(BaseModel):
    name: str = Field(description="Точка: пересечение осей «2/Б» или угол контура")
    dx_mm: float | None = Field(None, description="Отклонение по X (вдоль буквенных осей), мм, со знаком")
    dy_mm: float | None = Field(None, description="Отклонение по Y, мм, со знаком")
    dz_mm: float | None = Field(None, description="Отклонение по высоте, мм, со знаком")


class Measurements(BaseModel):
    points: list[PointMeasure] = Field(default_factory=list)


class Tolerance(BaseModel):
    plan_mm: float | None = Field(None, description="Допуск в плане (смещение осей), мм")
    height_mm: float | None = Field(None, description="Допуск по высоте (отметки), мм")
    citation: str | None = None


# «2/Б», «2 / Б», «2-Б»; дефис — только слитно, иначе «Y -3» читалось бы как точка Y/3
_AXIS_PT = re.compile(r"(?<![\w.,])(\d+|[А-ЯA-Z]\d?)(?:\s*/\s*|-)(\d+|[А-ЯA-Z]\d?)(?![\w.,])")
_DEV = re.compile(r"(?<![\w])([xyzхуz]|икс\w*|игрек\w*|зет\w*|высот\w*|отметк\w*)\s*[:=]?\s*(плюс|минус|[+\-−])?\s*"
                  r"(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_LATIN = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


def point_key(name: str) -> frozenset[str]:
    """«1/А», «А-1», «колонна 1/A» (латинская A), «т1» → один ключ: порядок осей и написание не важны."""
    s = re.sub(r"колонн\w*|ось|оси|точк\w*|пересечени\w*", " ", name.upper().translate(_LATIN), flags=re.IGNORECASE)
    parts = [x for x in re.split(r"[\s/\\\-–—:,]+", s) if x]
    return frozenset(parts)


def heuristic_measurements(text: str) -> Measurements:
    """«ось 2/Б: X +8, Y -3, высота -5; 3/В x 0 y +12» → отклонения по точкам."""
    out = []
    parts = list(_AXIS_PT.finditer(text))
    for i, m in enumerate(parts):
        seg = text[m.end(): parts[i + 1].start() if i + 1 < len(parts) else len(text)]
        pm = PointMeasure(name=f"{m.group(1).upper()}/{m.group(2).upper()}")
        for d in _DEV.finditer(seg):
            axis = d.group(1).lower()
            sign = -1 if (d.group(2) or "").lower() in ("минус", "-", "−") else 1
            val = sign * float(d.group(3).replace(",", "."))
            if axis in ("x", "х") or axis.startswith("икс"):
                pm.dx_mm = val
            elif axis in ("y", "у") or axis.startswith("игрек"):
                pm.dy_mm = val
            else:
                pm.dz_mm = val
        if pm.dx_mm is not None or pm.dy_mm is not None or pm.dz_mm is not None:
            out.append(pm)
    return Measurements(points=out)


def _norm_tolerance(db: Session, gateway, embedder, org_id: uuid.UUID, element: str) -> Tolerance | None:
    from bis.llm.extract import extract
    from bis.rag.search import hybrid_search

    hits = hybrid_search(db, embedder, f"допускаемые отклонения {element} смещение осей отметки мм",
                         org_id=org_id, kinds=["norm"], k=4)
    if not hits or gateway is None:
        return None
    ctx = "\n\n".join(f"[{h.citation}] {h.content[:1200]}" for h in hits)
    res = extract(gateway, Tolerance, ctx, task="extract",
                  instructions=f"Найди в пунктах норм допускаемые отклонения для конструкции «{element}»: смещение осей "
                               "в плане и отклонение отметок, в мм. В citation — документ и пункт, откуда взято. "
                               "Если в тексте нет — null.")
    return res.data if res.data and (res.data.plan_mm or res.data.height_mm) else None


def draft_scheme(db: Session, storage: Storage, gateway, embedder, org_id: uuid.UUID, *, title: str, element: str,
                 x_axes: list[dict], y_axes: list[dict], contour: list[list[float]] | None = None,
                 measurements_text: str = "", tolerance_plan_mm: float | None = None,
                 tolerance_height_mm: float | None = None, visual_note: str = "", design_z: float | None = None,
                 object_id: uuid.UUID | None = None, code: str = "", actor: str = "") -> Document:
    """Исполнительная схема без геодезической съёмки: геометрия из проекта (оси, контур, отметка), допуски из
    норматива (или указанные), факт — из замеров прораба (рулетка, нивелир; текст или голос)."""
    from bis.cad.scheme import Axis, SurveyPoint
    from bis.llm.extract import extract

    xs = {a["name"]: float(a["coord"]) for a in x_axes}
    ys = {a["name"]: float(a["coord"]) for a in y_axes}
    if contour and len(contour) >= 3:
        pts = [SurveyPoint(name=f"т{i + 1}", design_x=float(p[0]), design_y=float(p[1]), design_z=design_z)
               for i, p in enumerate(contour)]
    else:
        pts = [SurveyPoint(name=f"{xn}/{yn}", design_x=x, design_y=y, design_z=design_z)
               for xn, x in xs.items() for yn, y in ys.items()]
    if not pts:
        raise ValueError("Укажите оси или контур конструкции по проекту")

    notes = []
    if measurements_text.strip():
        res = extract(gateway, Measurements, measurements_text, task="extract",
                      instructions="Это замеры прораба по исполнительной съёмке рулеткой/нивелиром. Для каждой точки "
                                   "(пересечение осей «2/Б» или «т1») верни отклонения факт−проект в мм со знаком: dx по X "
                                   "(цифровые оси), dy по Y (буквенные оси), dz по высоте. Только то, что сказано.",
                      fallback=heuristic_measurements)
        # модель дополняет правила: явные «X +8» из правил надёжнее, модель понимает свободную речь
        by_name: dict[frozenset, PointMeasure] = {}
        for m in [*(res.data.points if res.data else []), *heuristic_measurements(measurements_text).points]:
            cur = by_name.setdefault(point_key(m.name), PointMeasure(name=m.name))
            for f in ("dx_mm", "dy_mm", "dz_mm"):
                if getattr(m, f) is not None:
                    setattr(cur, f, getattr(m, f))
        for p in pts:
            m = by_name.get(point_key(p.name))
            if not m:
                continue
            if m.dx_mm is not None or m.dy_mm is not None:
                p.fact_x = p.design_x + (m.dx_mm or 0) / 1000
                p.fact_y = p.design_y + (m.dy_mm or 0) / 1000
            if m.dz_mm is not None and p.design_z is not None:
                p.fact_z = p.design_z + m.dz_mm / 1000
        notes.append("Фактические отклонения — по замерам на месте (рулетка, нивелир), без геодезической съёмки.")
    else:
        notes.append("Схема по проекту: фактические значения вносятся по замерам.")

    tol = None
    if tolerance_plan_mm is None or tolerance_height_mm is None:
        try:
            tol = _norm_tolerance(db, gateway, embedder, org_id, element)
        except Exception:  # noqa: BLE001 — без найденной нормы — допуски по умолчанию
            tol = None
    plan = tolerance_plan_mm or (tol.plan_mm if tol and tol.plan_mm else 15)
    height = tolerance_height_mm or (tol.height_mm if tol and tol.height_mm else 10)
    if tol and tol.citation:
        notes.append(f"Допуски: в плане ±{plan:g} мм, по высоте ±{height:g} мм — {tol.citation}.")
    elif tolerance_plan_mm is None:
        notes.append(f"Допуски ±{plan:g}/±{height:g} мм приняты по умолчанию — уточните по проекту и СП.")
    if visual_note.strip():
        notes.append(f"Визуальный контроль (фото/видео): {visual_note.strip()[:300]}")

    org = db.get(Organization, org_id)
    obj = db.get(ConstructionObject, object_id) if object_id else None
    data = SchemeData(title=title, object_name=obj.name if obj else "", organization=org.name if org else "", code=code,
                      x_axes=[Axis(name=k, coord=v) for k, v in xs.items()], y_axes=[Axis(name=k, coord=v) for k, v in ys.items()],
                      contour=[(float(p[0]), float(p[1])) for p in (contour or [])], points=pts,
                      tolerance_plan_mm=plan, tolerance_height_mm=height, notes=notes)
    return create_scheme(db, storage, org_id, data, actor=actor, object_id=object_id)

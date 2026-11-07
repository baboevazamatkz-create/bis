"""MCP-сервер «bis-cad»: ИИ рисует в AutoCAD и nanoCAD.

Запуск: `bis cad-mcp` (stdio). Подключение в Claude Desktop / Claude Code — см. docs/CAD_MCP.md.
Работает на компьютере, где установлен CAD: подключается к запущенному AutoCAD или nanoCAD через COM;
если CAD не запущен — рисует в DXF-файл, который открывается в любом из них.

Все координаты — в миллиметрах (1 мм чертежа = 1 мм натуры, как в исполнительных схемах).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from bis.cad import bridge

INSTRUCTIONS = (
    "Инструменты рисуют в открытом чертеже AutoCAD или nanoCAD (или в DXF, если CAD не запущен). "
    "Координаты в миллиметрах. Для исполнительной схемы по проекту и замерам используйте cad_executive_scheme; "
    "для произвольного чертежа — cad_axes, cad_line, cad_polyline, cad_text, cad_dimension. Сохраняйте cad_save."
)

_state: dict[str, Any] = {"cad": None}


def _cad() -> bridge.Backend:
    if _state["cad"] is None:
        _state["cad"] = bridge.connect("auto")
    return _state["cad"]


def status() -> dict:
    cad = _cad()
    out = {"backend": cad.name, "entities": cad.count(), "file": getattr(cad, "path", None),
           "hint": "Рисую в DXF-файл: AutoCAD/nanoCAD не подключён — причины в errors; после исправления вызовите cad_connect"
           if cad.name == "dxf" else f"Подключено к {cad.name}"}
    if cad.name == "dxf" and bridge.LAST_ERRORS:
        out["errors"] = list(bridge.LAST_ERRORS)
    return out


def connect(target: str = "auto") -> dict:
    """target: auto | autocad | nanocad | dxf"""
    _state["cad"] = bridge.connect(target)
    return status()


def axes(x_axes: list[dict], y_axes: list[dict]) -> dict:
    bridge.draw_axes(_cad(), x_axes, y_axes)
    _cad().zoom()
    return status()


def line(x1: float, y1: float, x2: float, y2: float, layer: str = "BIS") -> dict:
    cad = _cad()
    cad.layer(layer)
    cad.line((x1, y1), (x2, y2), layer)
    return status()


def polyline(points: list[list[float]], closed: bool = False, layer: str = "BIS") -> dict:
    cad = _cad()
    cad.layer(layer)
    cad.polyline([(float(p[0]), float(p[1])) for p in points], closed, layer)
    return status()


def circle(x: float, y: float, radius: float, layer: str = "BIS") -> dict:
    cad = _cad()
    cad.layer(layer)
    cad.circle((x, y), radius, layer)
    return status()


def text(text: str, x: float, y: float, height: float = 250, layer: str = "BIS_TEXT", rotation: float = 0) -> dict:
    cad = _cad()
    cad.layer(layer)
    cad.text(text, (x, y), height, layer, rotation)
    return status()


def dimension(x1: float, y1: float, x2: float, y2: float, offset_x: float, offset_y: float, layer: str = "BIS_DIM") -> dict:
    cad = _cad()
    cad.layer(layer)
    cad.dimension((x1, y1), (x2, y2), (offset_x, offset_y), layer)
    return status()


def executive_scheme(scheme: dict) -> dict:
    """Исполнительная схема BIS (оси, контур, точки проект/факт, отклонения, допуски, штамп) — в CAD.

    scheme: {title, object_name, organization, code, x_axes:[{name, coord(м)}], y_axes, contour:[[x,y](м)],
    points:[{name, design_x, design_y, fact_x?, fact_y?, design_z?, fact_z?} (м)], tolerance_plan_mm, tolerance_height_mm, notes}
    Факт может отсутствовать — тогда схема по проекту с пустыми графами для замеров."""
    from bis.cad.scheme import SchemeData, build_dxf, check_points, dxf_bytes, summary

    data = SchemeData.model_validate(scheme)
    doc, checks, scale = build_dxf(data)
    out = Path(tempfile.gettempdir()) / f"bis-scheme-{abs(hash(data.title)) % 100000}.dxf"
    out.write_bytes(dxf_bytes(doc))
    cad = _cad()
    cad.open_file(str(out))
    cad.zoom()
    return {"file": str(out), "opened_in": cad.name, "scale": f"1:{scale}", **summary(check_points(data), data)}


def diagnose() -> dict:
    """Почему не удаётся подключиться к AutoCAD/nanoCAD."""
    info = bridge.diagnose()
    _state["cad"] = None
    return info


def save(path: str = "") -> dict:
    return {"saved": _cad().save(path), **status()}


TOOLS = [
    ("cad_status", status, "Что подключено (AutoCAD, nanoCAD или DXF-файл) и сколько объектов в чертеже."),
    ("cad_connect", connect, "Подключиться к CAD: auto | autocad | nanocad | dxf."),
    ("cad_axes", axes, "Сетка разбивочных осей с марками и размерами. x_axes/y_axes: [{name, coord}] — coord в мм."),
    ("cad_line", line, "Отрезок (мм)."),
    ("cad_polyline", polyline, "Полилиния по точкам [[x, y], …] (мм), closed — замкнуть контур."),
    ("cad_circle", circle, "Окружность (мм)."),
    ("cad_text", text, "Однострочный текст (высота в мм чертежа)."),
    ("cad_dimension", dimension, "Параллельный размер между точками; offset — точка положения размерной линии (мм)."),
    ("cad_executive_scheme", executive_scheme, executive_scheme.__doc__ or ""),
    ("cad_save", save, "Сохранить чертёж (путь .dwg или .dxf; пусто — текущий файл)."),
    ("cad_diagnose", diagnose, "Диагностика подключения к AutoCAD/nanoCAD: pywin32, реестр, процессы, права, ошибки."),
]


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
    server = MCPServer("bis-cad", instructions=INSTRUCTIONS)
    for name, fn, desc in TOOLS:
        server.tool(name=name, description=desc)(fn)
    return server


def main() -> None:
    build_server().run()

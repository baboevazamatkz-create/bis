"""MCP-сервер «bis-cad»: ИИ рисует в AutoCAD и nanoCAD.

Запуск: `bis cad-mcp` (stdio). Подключение в Claude Desktop / Claude Code — см. docs/CAD_MCP.md.
Работает на компьютере, где установлен CAD: подключается к запущенному AutoCAD или nanoCAD через COM;
если CAD не запущен — рисует в DXF-файл, который открывается в любом из них.

Все координаты — в миллиметрах (1 мм чертежа = 1 мм натуры, как в исполнительных схемах).
"""

from __future__ import annotations

import functools
import tempfile
import time
from pathlib import Path
from typing import Any

from bis.cad import bridge

INSTRUCTIONS = """Инструменты рисуют в открытом чертеже AutoCAD или nanoCAD (или в DXF, если CAD не запущен). Координаты в мм.

Правила, чтобы чертёж был аккуратным:
1. Таблицы, ведомости, листы согласования — только cad_table; основная надпись (штамп) — только cad_title_block.
   Не собирайте таблицы из cad_line + cad_text: ширину надписи вы не знаете, текст наедет на линии и соседей.
   cad_table сам центрирует текст в ячейке по высоте, переносит длинный на вторую строку или сужает его.
2. Ширины колонок задавайте по самому длинному тексту: примерно 0.8 × высота шрифта × число символов + 2 мм.
   Высоты шрифта по ГОСТ: 2.5 (таблицы), 3.5 (названия), 5 (обозначение документа) — в мм листа.
3. Масштаб: чертёж в мм натуры, а таблицы и штамп — в мм листа; для листа 1:100 передайте scale=100.
4. Отдельные надписи (cad_text) — с max_width, если рядом линии или другие надписи.
5. После рисования вызовите cad_check: он находит наезжающие надписи. Исправьте и проверьте снова.
6. Для исполнительной схемы по проекту и замерам — cad_executive_scheme (оси, контур, отклонения, штамп сразу).
7. Сохранение — cad_save."""

_state: dict[str, Any] = {"cad": None}


def _cad() -> bridge.Backend:
    """Подключение при каждом обращении, пока работаем в DXF: CAD могли запустить позже."""
    cad = _state["cad"]
    if cad is None or (cad.name == "dxf" and not _state.get("dxf_forced") and cad.count() == 0):
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
    _state["dxf_forced"] = target == "dxf"
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


def text(text: str, x: float, y: float, height: float = 250, layer: str = "BIS_TEXT", rotation: float = 0,
         max_width: float = 0, align: str = "left") -> dict:
    cad = _cad()
    cad.layer(layer)
    if (max_width or align != "left") and not rotation:
        from bis.cad.layout import Text, fit

        if max_width:
            lines, h, wf = fit(text, max_width + 2, height * 1.7, height)
            text, height = " ".join(lines), h
        else:
            wf = 1.0
        cad.fitted_text(Text(text, x, y, height, wf, align, max_width or None), layer)
    else:
        cad.text(text, (x, y), height, layer, rotation)
    return status()


def _draw(lay, layer: str) -> int:
    cad = _cad()
    cad.layer(layer)
    cad.layer(layer + "_TEXT")
    for a, b in _dedupe(lay.lines):
        cad.line(a, b, layer)
    for t in lay.texts:
        cad.fitted_text(t, layer + "_TEXT")
    return len(lay.texts)


def _dedupe(lines):
    seen, out = set(), []
    for a, b in lines:
        key = tuple(sorted([(round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))]))
        if key[0] != key[1] and key not in seen:
            seen.add(key)
            out.append((a, b))
    return out


def table(x: float, y: float, columns: list[float], rows: list[list[Any]], row_height: float = 8,
          row_heights: list[float] | None = None, text_height: float = 2.5, header: bool = True,
          scale: float = 1, layer: str = "BIS_TABLE") -> dict:
    """Таблица от левого верхнего угла (x, y) вниз. columns — ширины колонок (мм листа), rows — строки,
    ячейка — текст или {"text", "span" (объединить колонок), "align": left|center}. Текст сам подгоняется под ячейку."""
    from bis.cad.layout import scaled
    from bis.cad.layout import table as build

    lay = scaled(build(x, y, columns, rows, row_heights or row_height, text_height, header), scale, (x, y))
    n = _draw(lay, layer)
    _cad().zoom()
    width, height = sum(columns) * scale, sum(row_heights or [row_height] * len(rows)) * scale
    return {"texts": n, "bottom_right": [x + width, y - height], **status()}


def title_block(x: float, y: float, code: str = "", object: str = "", title: str = "", stage: str = "",  # noqa: A002
                sheet: str = "1", sheets: str = "1", organization: str = "", signatures: list[dict] | None = None,
                scale: float = 1, layer: str = "BIS_STAMP") -> dict:
    """Основная надпись по форме 3 ГОСТ Р 21.101 (185×55 мм листа), правый нижний угол — (x, y).
    signatures: [{role, name, date}] до 5 строк (Разраб., Пров., ГИП, Н.контр., Утв. и т.п.)."""
    from bis.cad.layout import scaled
    from bis.cad.layout import title_block as build

    f = {"code": code, "object": object, "title": title, "stage": stage, "sheet": sheet, "sheets": sheets,
         "organization": organization, "signatures": signatures or []}
    lay = scaled(build(x, y, f), scale, (x, y))
    n = _draw(lay, layer)
    _cad().zoom()
    return {"texts": n, "size_mm": [185 * scale, 55 * scale], **status()}


def check() -> dict:
    """Наезжающие друг на друга надписи в чертеже."""
    from bis.cad.layout import overlaps

    boxes = _cad().text_boxes()
    found = overlaps(boxes)
    return {"texts": len(boxes), "overlaps": found[:40], "ok": not found,
            "hint": "Наложений нет" if not found else "Увеличьте ширину колонок или высоту строк, уменьшите шрифт, "
            "или перерисуйте таблицу через cad_table — он сам подгоняет текст"}


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
    ("cad_text", text, "Однострочный текст (высота в мм чертежа). max_width — не шире (сузит/уменьшит), align: left|center."),
    ("cad_table", table, table.__doc__ or ""),
    ("cad_title_block", title_block, title_block.__doc__ or ""),
    ("cad_check", check, "Проверка оформления: какие надписи наезжают друг на друга. Вызывайте после рисования."),
    ("cad_dimension", dimension, "Параллельный размер между точками; offset — точка положения размерной линии (мм)."),
    ("cad_executive_scheme", executive_scheme, executive_scheme.__doc__ or ""),
    ("cad_save", save, "Сохранить чертёж (путь .dwg или .dxf; пусто — текущий файл)."),
    ("cad_diagnose", diagnose, "Диагностика подключения к AutoCAD/nanoCAD: pywin32, реестр, процессы, права, ошибки."),
]


# COM-объекты AutoCAD привязаны к потоку, в котором созданы: все вызовы — через общий поток COM (bridge.in_com_thread).
BUSY = {-2147418111, -2147417846}  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER — AutoCAD занят
LOST = {-2147417848, -2147023174, -2147417842, -2147221008, -2147220995, -2147023170}  # связь потеряна


def _hresult(e: Exception) -> int | None:
    hr = getattr(e, "hresult", None)
    if hr is None and e.args and isinstance(e.args[0], int):
        hr = e.args[0]
    return hr


def guarded(fn):
    """Инструмент: в потоке COM; «AutoCAD занят» — повтор, потеря связи — переподключение."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        def call():
            reconnects = 0
            for _ in range(60):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    hr = _hresult(e)
                    if hr in BUSY:
                        time.sleep(0.25)
                        continue
                    if hr in LOST and reconnects < 2:
                        reconnects += 1
                        _state["cad"] = None
                        continue
                    if hr is not None:
                        raise _tool_error(f"Ошибка CAD ({hr}): {e}. Закройте диалоговые окна в CAD и повторите.") from e
                    if isinstance(e, bridge.CadError):
                        raise _tool_error(str(e)) from e
                    # объект AutoCAD отвалился (чертёж закрыт, CAD перезапущен) — переподключаемся
                    if isinstance(e, AttributeError) and "<unknown>" in str(e) and reconnects < 2:
                        reconnects += 1
                        _state["cad"] = None
                        continue
                    raise _tool_error(f"{type(e).__name__}: {e}") from e
            raise _tool_error("CAD занят (открыто диалоговое окно или идёт команда) — завершите её и повторите")
        return bridge.in_com_thread(call)
    return run


def _tool_error(message: str) -> Exception:
    """Ошибка, текст которой видит ИИ (иначе MCP передаёт только «Error executing tool»)."""
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:
        try:
            from mcp.server.fastmcp.exceptions import ToolError
        except ImportError:
            return bridge.CadError(message)
    return ToolError(message)


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer  # mcp 1.x
    server = MCPServer("bis-cad", instructions=INSTRUCTIONS)
    for name, fn, desc in TOOLS:
        server.tool(name=name, description=desc)(guarded(fn))
    return server


def main() -> None:
    build_server().run()

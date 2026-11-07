"""Мост к AutoCAD и nanoCAD: рисование прямо в открытом чертеже.

- AutoCAD и nanoCAD на Windows управляются через COM (ActiveX Automation): «AutoCAD.Application»,
  «nanoCAD.Application» — у nanoCAD API совместим с AutoCAD (ModelSpace.AddLine, AddText, AddDimAligned…).
- Если CAD не запущен или это не Windows — те же команды пишутся в DXF-файл (ezdxf), который потом открывается в CAD.

Координаты — в миллиметрах чертежа (как в исполнительных схемах BIS: 1 мм модели = 1 мм натуры).
"""

from __future__ import annotations

import contextlib
import logging
import math
import sys
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)
PROGIDS = {"autocad": "AutoCAD.Application", "nanocad": "nanoCAD.Application"}
ACI = {"red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5, "magenta": 6, "white": 7, "gray": 8}


class CadError(Exception):
    pass


class Backend(Protocol):
    name: str

    def layer(self, name: str, color: int = 7) -> None: ...
    def line(self, p1: tuple[float, float], p2: tuple[float, float], layer: str) -> None: ...
    def polyline(self, pts: list[tuple[float, float]], closed: bool, layer: str) -> None: ...
    def circle(self, c: tuple[float, float], r: float, layer: str) -> None: ...
    def text(self, s: str, pos: tuple[float, float], height: float, layer: str, rotation: float = 0) -> None: ...
    def dimension(self, p1: tuple[float, float], p2: tuple[float, float], at: tuple[float, float], layer: str) -> None: ...
    def save(self, path: str) -> str: ...
    def open_file(self, path: str) -> str: ...
    def count(self) -> int: ...
    def zoom(self) -> None: ...


class DxfBackend:
    """Без CAD: рисование в DXF-файл."""

    name = "dxf"

    def __init__(self, path: str | None = None):
        import ezdxf

        self.path = path
        self.doc = ezdxf.readfile(path) if path and Path(path).exists() else ezdxf.new("R2010", setup=True)
        self.msp = self.doc.modelspace()

    def layer(self, name: str, color: int = 7) -> None:
        if name not in self.doc.layers:
            self.doc.layers.add(name, color=color)

    def line(self, p1, p2, layer):
        self.msp.add_line(p1, p2, dxfattribs={"layer": layer})

    def polyline(self, pts, closed, layer):
        self.msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": layer})

    def circle(self, c, r, layer):
        self.msp.add_circle(c, r, dxfattribs={"layer": layer})

    def text(self, s, pos, height, layer, rotation=0):
        self.msp.add_text(s, height=height, rotation=rotation, dxfattribs={"layer": layer}).set_placement(pos)

    def dimension(self, p1, p2, at, layer):
        dim = self.msp.add_aligned_dim(p1=p1, p2=p2, distance=_offset(p1, p2, at), dxfattribs={"layer": layer})
        dim.render()

    def save(self, path: str) -> str:
        path = path or self.path or str(Path.cwd() / "bis-drawing.dxf")
        if not path.lower().endswith(".dxf"):
            path = str(Path(path).with_suffix(".dxf"))
        self.doc.saveas(path)
        self.path = path
        return path

    def open_file(self, path: str) -> str:
        import ezdxf

        self.doc = ezdxf.readfile(path)
        self.msp = self.doc.modelspace()
        self.path = path
        return path

    def count(self) -> int:
        return len(self.msp)

    def zoom(self) -> None:
        pass


def _offset(p1, p2, at) -> float:
    """Расстояние от размерной линии до точки `at` (со знаком) — для DXF-размера."""
    (x1, y1), (x2, y2), (x, y) = p1, p2, at
    length = math.hypot(x2 - x1, y2 - y1) or 1.0
    return ((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) / length


class ComBackend:
    """AutoCAD или nanoCAD через COM (только Windows, пакет pywin32)."""

    def __init__(self, product: str, app: Any = None):
        self.name = product
        if app is None:
            if sys.platform != "win32":
                raise CadError("Управление AutoCAD/nanoCAD доступно на Windows")
            try:
                import win32com.client
            except ImportError as e:
                raise CadError("Не установлен pywin32: pip install pywin32") from e
            app = _attach(product, win32com.client)
        self.app = app
        try:
            self.doc = app.ActiveDocument
        except Exception:  # noqa: BLE001 — нет открытых чертежей
            self.doc = app.Documents.Add()

    @staticmethod
    def _pt(x: float, y: float, z: float = 0.0):
        try:
            import pythoncom
            import win32com.client

            return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(x), float(y), float(z)))
        except ImportError:  # тесты без Windows
            return (float(x), float(y), float(z))

    @staticmethod
    def _arr(values: list[float]):
        try:
            import pythoncom
            import win32com.client

            return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in values))
        except ImportError:
            return tuple(float(v) for v in values)

    @property
    def ms(self):
        return self.doc.ModelSpace

    def layer(self, name, color=7):
        lay = self.doc.Layers.Add(name)
        lay.color = color

    def _on(self, ent, layer):
        ent.Layer = layer
        return ent

    def line(self, p1, p2, layer):
        self._on(self.ms.AddLine(self._pt(*p1), self._pt(*p2)), layer)

    def polyline(self, pts, closed, layer):
        flat = [c for p in pts for c in p]
        pl = self.ms.AddLightWeightPolyline(self._arr(flat))
        pl.Closed = closed
        self._on(pl, layer)

    def circle(self, c, r, layer):
        self._on(self.ms.AddCircle(self._pt(*c), float(r)), layer)

    def text(self, s, pos, height, layer, rotation=0):
        t = self.ms.AddText(s, self._pt(*pos), float(height))
        if rotation:
            t.Rotation = math.radians(rotation)
        self._on(t, layer)

    def dimension(self, p1, p2, at, layer):
        self._on(self.ms.AddDimAligned(self._pt(*p1), self._pt(*p2), self._pt(*at)), layer)

    def save(self, path):
        if path:
            self.doc.SaveAs(path)
            return path
        self.doc.Save()
        return str(self.doc.FullName)

    def open_file(self, path):
        self.doc = self.app.Documents.Open(str(path))
        return path

    def count(self):
        return int(self.ms.Count)

    def zoom(self):
        with contextlib.suppress(Exception):  # у части версий nanoCAD нет ZoomExtents у приложения
            self.app.ZoomExtents()


def registered_progids(product: str) -> list[str]:
    """ProgID из реестра: общий «AutoCAD.Application» и версионные «AutoCAD.Application.24.3» и т.п."""
    base = PROGIDS[product]
    found = []
    try:
        import winreg

        i = 0
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if name.lower().startswith(base.lower()):
                    found.append(name)
    except Exception:  # noqa: BLE001 — не Windows
        return [base]
    # сначала общий, затем версионные от новых к старым
    return sorted(set(found) | {base}, key=lambda n: (n.lower() != base.lower(), [-ord(c) for c in n]))


def _attach(product: str, client) -> Any:
    """Подключение к запущенному CAD: GetActiveObject по всем ProgID, затем Dispatch (подхватывает запущенный)."""
    errors = []
    progids = registered_progids(product)
    for progid in progids:
        try:
            return client.GetActiveObject(progid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{progid}: {_com_error(e)}")
    for progid in progids:
        try:
            return client.Dispatch(progid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{progid} (запуск): {_com_error(e)}")
    raise CadError(f"{product}: " + "; ".join(errors[:6]))


def _com_error(e: Exception) -> str:
    text = str(e)
    if "-2147221005" in text or "Invalid class string" in text or "Недопустимая строка с указанием класса" in text:
        return "не зарегистрирован (CAD не установлен или это версия LT без программного управления)"
    if "-2147221021" in text or "Operation unavailable" in text or "Операция недоступна" in text:
        return "не запущен или запущен с другими правами (от администратора)"
    return text[:160]


LAST_ERRORS: list[str] = []


def connect(target: str = "auto", dxf_path: str | None = None) -> Backend:
    """auto: запущенный AutoCAD, затем nanoCAD, иначе DXF-файл. Причины отказа — в LAST_ERRORS."""
    order = ["autocad", "nanocad"] if target == "auto" else [target] if target in PROGIDS else []
    LAST_ERRORS.clear()
    for product in order:
        try:
            return ComBackend(product)
        except CadError as e:
            LAST_ERRORS.append(str(e))
    if target not in ("auto", "dxf"):
        raise CadError("; ".join(LAST_ERRORS))
    return DxfBackend(dxf_path)


def diagnose() -> dict:
    """Почему не удаётся подключиться к CAD: платформа, pywin32, ProgID в реестре, запущенные процессы, права."""
    info: dict[str, Any] = {"platform": sys.platform, "python": sys.executable}
    try:
        import win32com.client  # noqa: F401

        info["pywin32"] = "установлен"
    except Exception as e:  # noqa: BLE001
        info["pywin32"] = f"НЕТ: {e}"
    info["progids"] = {p: registered_progids(p) for p in PROGIDS}
    try:
        import subprocess

        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20).stdout
        info["running"] = sorted({ln.split(",")[0].strip('"') for ln in out.splitlines()
                                  if any(k in ln.lower() for k in ("acad", "nanocad", "ncad"))})
    except Exception:  # noqa: BLE001
        info["running"] = "не удалось проверить"
    try:
        import ctypes

        info["this_process_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        pass
    cad = connect("auto")
    info["connected"] = cad.name
    info["errors"] = list(LAST_ERRORS)
    if cad.name != "dxf":
        with contextlib.suppress(Exception):
            info["document"] = str(cad.doc.Name)
    return info


def draw_axes(cad: Backend, x_axes: list[dict], y_axes: list[dict], *, bubble: float = 400, ext: float = 1500,
              layer: str = "BIS_AXES") -> None:
    """Сетка разбивочных осей с марками в кружках. Координаты осей — в мм."""
    cad.layer(layer, ACI["gray"])
    xs = [float(a["coord"]) for a in x_axes] or [0.0]
    ys = [float(a["coord"]) for a in y_axes] or [0.0]
    y0, y1 = min(ys) - ext, max(ys) + ext
    x0, x1 = min(xs) - ext, max(xs) + ext
    for a in x_axes:
        x = float(a["coord"])
        cad.line((x, y0), (x, y1), layer)
        cad.circle((x, y1 + bubble), bubble, layer)
        cad.text(str(a["name"]), (x - bubble * 0.35, y1 + bubble * 0.6), bubble * 0.8, layer)
    for a in y_axes:
        y = float(a["coord"])
        cad.line((x0, y), (x1, y), layer)
        cad.circle((x0 - bubble, y), bubble, layer)
        cad.text(str(a["name"]), (x0 - bubble * 1.35, y - bubble * 0.4), bubble * 0.8, layer)
    for a, b in zip(x_axes, x_axes[1:], strict=False):
        cad.dimension((float(a["coord"]), y1), (float(b["coord"]), y1), (float(a["coord"]), y1 + bubble * 3), layer)
    for a, b in zip(y_axes, y_axes[1:], strict=False):
        cad.dimension((x0, float(a["coord"])), (x0, float(b["coord"])), (x0 - bubble * 3, float(a["coord"])), layer)

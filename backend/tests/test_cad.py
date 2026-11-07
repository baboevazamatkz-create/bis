"""Исполнительные схемы: разбор съёмки, отклонения, DXF, предпросмотр, API, агент."""

from __future__ import annotations

import io

import ezdxf

from bis.cad.scheme import (
    Axis,
    SchemeData,
    build_dxf,
    check_points,
    dxf_bytes,
    parse_survey,
    summary,
)
from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall

SURVEY = "Точка;Xпр;Yпр;Xф;Yф;Hпр;Hф\n1;0;0;0,008;-0,005;-3,200;-3,195\n2;24;0;24,012;0,004;-3,200;-3,214\n" \
         "3;24;18;23,990;18,021;-3,200;-3,203\n4;0;18;-0,003;18,006;-3,200;-3,199\n"


def scheme(points=None) -> SchemeData:
    return SchemeData(title="Схема плиты Пм1", object_name="Корпус 1", code="ИГС-01",
                      x_axes=[Axis(name=str(i + 1), coord=i * 6) for i in range(5)],
                      y_axes=[Axis(name=n, coord=c) for n, c in zip("АБВГ", [0, 6, 12, 18], strict=True)],
                      contour=[(0, 0), (24, 0), (24, 18), (0, 18)],
                      points=points or parse_survey(SURVEY.encode(), "s.csv"))


def test_parse_survey_csv_and_geodetic_swap():
    pts = parse_survey(SURVEY.encode(), "survey.csv")
    assert len(pts) == 4 and pts[1].fact_x == 24.012 and pts[1].fact_z == -3.214
    swapped = parse_survey(SURVEY.encode(), "survey.csv", geodetic=True)
    assert swapped[1].design_x == 0 and swapped[1].design_y == 24
    plain = parse_survey(b"A,1.0,2.0,1.004,2.0\n", "noheader.txt")
    assert plain[0].name == "A" and plain[0].fact_x == 1.004


def test_deviation_checks():
    checks = {c.name: c for c in check_points(scheme())}
    assert (checks["2"].dx_mm, checks["2"].dy_mm, checks["2"].dz_mm) == (12, 4, -14)
    assert checks["2"].plan_ok is True and checks["2"].height_ok is False
    assert checks["3"].plan_ok is False  # +21 мм > 15
    s = summary(list(checks.values()), scheme())
    assert s["out_of_tolerance"] == ["2", "3"] and s["max_plan_mm"] == 21 and s["max_height_mm"] == 14


def test_dxf_structure():
    doc, checks, scale = build_dxf(scheme())
    assert scale == 200
    data = dxf_bytes(doc)
    reread = ezdxf.read(io.StringIO(data.decode()))
    msp = reread.modelspace()
    layers = {e.dxf.layer for e in msp}
    assert {"BIS_AXES", "BIS_CONTOUR", "BIS_POINTS", "BIS_DEV_OK", "BIS_DEV_BAD", "BIS_FRAME"} <= layers
    texts = [e.dxf.text for e in msp.query("TEXT")]
    assert "+21" in texts and "ИГС-01" in texts and "-3.214" in texts
    bad = [e.dxf.text for e in msp.query("TEXT") if e.dxf.layer == "BIS_DEV_BAD"]
    assert "+21" in bad and "+8" not in bad
    assert reread.units == ezdxf.units.MM


def _object_id(client, auth):
    return client.get("/api/objects", headers=auth).json()[0]["id"]


def test_scheme_api(client, auth):
    parsed = client.post("/api/schemes/survey", headers=auth, files={"file": ("s.csv", SURVEY.encode(), "text/csv")}).json()
    assert len(parsed["points"]) == 4
    body = scheme().model_dump(mode="json") | {"object_id": _object_id(client, auth), "points": parsed["points"]}
    r = client.post("/api/schemes", headers=auth, json=body)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["kind"] == "scheme" and doc["data"]["summary"]["out_of_tolerance"] == ["2", "3"]
    assert doc["issues"][0]["level"] == "warning"
    preview = client.get(f"/api/documents/{doc['id']}/preview", headers=auth)
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/svg") and b"<svg" in preview.content[:300]
    dxf = client.get(f"/api/documents/{doc['id']}/file", headers=auth).content
    assert b"BIS_DEV_BAD" in dxf
    assert client.get(f"/api/documents/{doc['id']}/dwg", headers=auth).status_code == 404  # без ODA


def test_cad_agent_from_survey_document(client, auth):
    up = client.post("/api/documents/upload", headers=auth, data={"kind": "other", "title": "Съёмка плиты", "index": "false"},
                     files={"file": ("survey.csv", SURVEY.encode(), "text/csv")}).json()
    pts = parse_survey(SURVEY.encode(), "s.csv")
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("1", "parse_survey_document", {"document_id": up["id"]})]),
        Message("assistant", tool_calls=[ToolCall("2", "create_executive_scheme", {
            "title": "Исполнительная схема плиты Пм1", "object_id": _object_id(client, auth),
            "points": [p.model_dump() for p in pts], "x_axes": [{"name": "1", "coord": 0}, {"name": "5", "coord": 24}],
            "y_axes": [{"name": "А", "coord": 0}, {"name": "Г", "coord": 18}]})]),
        "Схема построена: М 1:200, превышение допусков в точках 2 и 3.",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    data = client.post("/api/chat", headers=auth, json={"message": "Построй исполнительную схему по съёмке плиты"}).json()
    assert data["agent"] == "cad"
    assert data["documents"] and data["documents"][0]["kind"] == "scheme"
    result = [m for m in llm.calls[2]["messages"] if m.role == "tool"][-1].content
    assert '"out_of_tolerance": ["2", "3"]' in result and '"scale": "1:200"' in result


def test_scheme_without_survey_from_project_and_measurements(client, auth):
    obj = client.get("/api/objects", headers=auth).json()[0]
    r = client.post("/api/schemes/draft", headers=auth, json={
        "title": "Исполнительная схема колонн, этаж 2", "element": "колонны", "object_id": obj["id"],
        "x_axes": [{"name": "1", "coord": 0}, {"name": "2", "coord": 6}], "y_axes": [{"name": "А", "coord": 0}, {"name": "Б", "coord": 6}],
        "measurements_text": "1/А: X +8, Y -3; 2/Б x минус 22 y 4", "tolerance_plan_mm": 15, "tolerance_height_mm": 10,
        "visual_note": "по фото армирование выполнено, фиксаторы установлены"})
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["summary"]["measured"] == 2 and data["summary"]["out_of_tolerance"] == ["2/Б"]
    assert "Замерено 2 из 4" in data["summary"]["conclusion"]
    assert any("рулетка" in n for n in data["input"]["notes"]) and any("Визуальный контроль" in n for n in data["input"]["notes"])
    empty = client.post("/api/schemes/draft", headers=auth, json={"x_axes": [{"name": "1", "coord": 0}],
                                                                  "y_axes": [{"name": "А", "coord": 0}]}).json()
    assert "не внесены" in empty["data"]["summary"]["conclusion"]


def test_cad_bridge_dxf_and_com(tmp_path):
    from bis.cad import bridge, mcp_server

    cad = bridge.DxfBackend()
    bridge.draw_axes(cad, [{"name": "1", "coord": 0}, {"name": "2", "coord": 6000}], [{"name": "А", "coord": 0}])
    cad.polyline([(0, 0), (6000, 0), (6000, 400)], True, "BIS")
    assert cad.count() > 5 and cad.save(str(tmp_path / "x.dxf")).endswith(".dxf")

    class Ent:
        def __init__(self):
            self.Layer = ""

    class MS:
        def __init__(self):
            self.calls = []
            self.Count = 0

        def __getattr__(self, name):
            def add(*a):
                self.calls.append(name)
                self.Count += 1
                return Ent()
            return add

    class LayerTable:
        def Add(self, name):  # noqa: N802 — COM
            return Ent()

    class Doc:
        ModelSpace = MS()
        Layers = LayerTable()

    class App:
        ActiveDocument = Doc()

    com = bridge.ComBackend("nanocad", app=App())
    bridge.draw_axes(com, [{"name": "1", "coord": 0}, {"name": "2", "coord": 6000}], [{"name": "А", "coord": 0}])
    com.text("Схема", (0, 0), 250, "BIS")
    assert {"AddLine", "AddCircle", "AddText", "AddDimAligned"} <= set(App.ActiveDocument.ModelSpace.calls)

    mcp_server._state["cad"] = bridge.DxfBackend()
    res = mcp_server.executive_scheme({"title": "т", "points": [{"name": "1", "design_x": 0, "design_y": 0, "fact_x": 0.01, "fact_y": 0}]})
    assert res["file"].endswith(".dxf") and res["opened_in"] == "dxf"
    assert len(mcp_server.TOOLS) == 14


def test_mcp_tools_run_in_one_com_thread_with_retry():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from bis.cad import mcp_server

    seen = set()
    calls = {"n": 0}

    def tool():
        seen.add(threading.current_thread().name)
        calls["n"] += 1
        if calls["n"] < 3:  # AutoCAD занят
            raise RuntimeError(-2147418111, "Call was rejected by callee.", None, None)
        return "ok"

    g = mcp_server.guarded(tool)
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(lambda _: g(), range(4))) == ["ok"] * 4
    assert len(seen) == 1 and next(iter(seen)).startswith("bis-com")


def test_mcp_errors_reach_model_and_lost_document_reconnects():
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    from bis.cad import bridge, mcp_server

    def not_running():
        raise bridge.CadError("autocad не запущен")

    with pytest.raises(ToolError, match="autocad не запущен"):
        mcp_server.guarded(not_running)()

    calls = {"n": 0}

    def stale():  # объект чертежа отвалился: win32com даёт AttributeError <unknown>.Save
        calls["n"] += 1
        if calls["n"] == 1:
            raise AttributeError("<unknown>.Save")
        return "saved"

    assert mcp_server.guarded(stale)() == "saved"


def test_com_save_new_drawing_goes_to_documents(tmp_path, monkeypatch):
    from bis.cad.bridge import ComBackend

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    class Doc:
        Name, Path, FullName = "Чертеж1.dwg", "", ""
        saved_as = None

        def SaveAs(self, p):
            Doc.saved_as = p

    class App:
        ActiveDocument = Doc()

    path = ComBackend("autocad", app=App()).save("")
    assert path.endswith("Чертеж1.dwg") and Doc.saved_as == path and (tmp_path / "Documents" / "BIS").is_dir()


def test_table_and_title_block_fit_text_without_overlaps():
    from bis.cad import bridge, mcp_server
    from bis.cad.layout import fit, text_width

    lines, h, wf = fit("Вед. инженер ОКС ЦЭС ПАО «Якутскэнерго»", 70, 8, 2.5)
    assert len(lines) == 2 and all(text_width(ln, h, wf) <= 68 for ln in lines)
    lines, h, wf = fit("Подпись", 15, 8, 2.5)
    assert text_width(lines[0], h, wf) <= 13

    mcp_server._state.update(cad=bridge.DxfBackend(), dxf_forced=True)
    rows = [["Должность", "Фамилия", "Подпись", "Дата"],
            ["Вед. инженер ОКС ЦЭС ПАО «Якутскэнерго»", "Федоров П.П.", "", "02.09.22"],
            [{"text": "Объединённая ячейка на всю ширину таблицы", "span": 4}]]
    out = mcp_server.table(0, 0, [70, 30, 15, 15], rows, scale=100)
    assert out["bottom_right"] == [13000, -2400]
    mcp_server.title_block(30000, -5000, code="84.801/20-02-АС", object="Реконструкция ПС 110 кВ Хандыга с расширением ОРУ 110 кВ",
                           title="Исполнительная схема №3", stage="ИД", organization="ООО «Строй Газ Инвест»",
                           signatures=[{"role": "Разраб.", "name": "Ахматов Ж.И.", "date": "02.09.22"}], scale=100)
    assert mcp_server.check()["ok"]
    mcp_server.text("Наезд", 100, -400, 250)  # поверх «Должность»
    mcp_server.text("Должность поверх", 100, -400, 250)
    assert not mcp_server.check()["ok"]
    mcp_server._state.update(cad=None, dxf_forced=False)


def test_open_in_cad_runs_in_com_thread(client, auth, db, demo, monkeypatch):
    import threading

    from bis.cad import bridge
    from bis.services.documents import save_document
    from bis.services.storage import get_storage

    doc = save_document(db, get_storage(), org_id=demo.id, kind="scheme", title="Схема", data={}, file_bytes=b"0\nEOF\n",
                        file_name="схема.dxf", created_by="test", mime="application/dxf")
    db.commit()
    seen = {}

    class FakeCad:
        name = "autocad"

        def open_file(self, path):
            seen["thread"] = threading.current_thread().name
            seen["path"] = path

        def zoom(self):
            pass

    monkeypatch.setattr(bridge, "connect", lambda target="auto": FakeCad())
    r = client.post(f"/api/documents/{doc.id}/open-in-cad", headers=auth)
    assert r.status_code == 200 and r.json()["opened_in"] == "autocad"
    assert seen["thread"].startswith("bis-com") and seen["path"].endswith(".dxf")

    monkeypatch.setattr(bridge, "connect", lambda target="auto": bridge.DxfBackend())
    bridge.LAST_ERRORS[:] = ["autocad: не запущен"]
    r = client.post(f"/api/documents/{doc.id}/open-in-cad", headers=auth)
    assert r.status_code == 400 and "не запущен" in r.json()["detail"]


def test_draft_scheme_merges_model_and_rules(db, demo, monkeypatch):
    """Модель вернула только одну точку (и с латинской буквой) — остальные замеры берутся из правил, ничего не теряется."""
    from types import SimpleNamespace

    import bis.llm.extract as ex
    from bis.services.schemes import Measurements, PointMeasure, draft_scheme
    from bis.services.storage import get_storage

    monkeypatch.setattr(ex, "extract", lambda *a, **k: SimpleNamespace(data=Measurements(points=[PointMeasure(name="B-3", dz_mm=-5)])))
    doc = draft_scheme(db, get_storage(), None, None, demo.id, title="Схема", element="колонны",
                       x_axes=[{"name": str(i + 1), "coord": i * 6} for i in range(3)],
                       y_axes=[{"name": n, "coord": i * 6} for i, n in enumerate("АБВ")], design_z=3.3,
                       measurements_text="колонна 1/А по иксу плюс 8, по игреку минус 3; 2/Б икс минус 12; 3/В высота минус 5",
                       tolerance_plan_mm=15, tolerance_height_mm=10)
    s = doc.data["summary"]
    assert s["measured"] == 3 and s["max_plan_mm"] == 12 and s["max_height_mm"] == 5

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

import { useState } from "react";
import { api, DEMO, download, fetchBlob, upload, type DocumentRow } from "../api";
import { demoSurvey } from "../demo/mock";
import { Picker, useAsync, useObjects } from "../components";

interface Point {
  name: string; design_x: number; design_y: number; fact_x: number | null; fact_y: number | null;
  design_z: number | null; fact_z: number | null;
}

// «1:0, 2:6, 3:12» → [{name: "1", coord: 0}, …]
const parseAxes = (s: string) => s.split(/[,;]/).map((x) => x.trim()).filter(Boolean).map((x) => {
  const [name, coord] = x.split(":");
  return { name: name.trim(), coord: Number((coord ?? "").replace(",", ".")) };
}).filter((a) => a.name && !Number.isNaN(a.coord));

export default function Schemes() {
  const objects = useObjects();
  const [objectId, setObjectId] = useState("");
  const [points, setPoints] = useState<Point[]>([]);
  const [geodetic, setGeodetic] = useState(false);
  const [form, setForm] = useState({ title: "Исполнительная схема", code: "", surveyor: "", checker: "",
    xAxes: "1:0, 2:6, 3:12, 4:18, 5:24", yAxes: "А:0, Б:6, В:12, Г:18", tolPlan: "15", tolHeight: "10", contour: true });
  const [doc, setDoc] = useState<DocumentRow | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const { busy, run, Alerts } = useAsync();

  async function parse(file: File) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("geodetic", String(geodetic));
    const r = await run(() => upload<{ points: Point[] }>("/schemes/survey", fd));
    if (r) setPoints(r.points);
  }

  async function build() {
    const contour = form.contour && points.length >= 3 ? points.map((p) => [p.design_x, p.design_y]) : [];
    const d = await run(() => api<DocumentRow & { data: { summary: { conclusion: string } } }>("/schemes", {
      method: "POST",
      json: {
        title: form.title, code: form.code, surveyor: form.surveyor, checker: form.checker, object_id: objectId || null,
        x_axes: parseAxes(form.xAxes), y_axes: parseAxes(form.yAxes), contour, points,
        tolerance_plan_mm: Number(form.tolPlan), tolerance_height_mm: Number(form.tolHeight),
        date: new Date().toLocaleDateString("ru-RU"),
      },
    }));
    if (!d) return;
    setDoc(d);
    const blob = await run(() => fetchBlob(`/documents/${d.id}/preview`));
    if (blob) setPreview(URL.createObjectURL(blob));
  }

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const summary = (doc as unknown as { data?: { summary?: { conclusion: string; max_plan_mm: number; max_height_mm: number | null } } })?.data?.summary;

  return (
    <>
      <h1>Исполнительные схемы</h1>
      <p className="muted">Файл съёмки геодезиста (CSV/TXT/XLSX: точка, Xпр, Yпр, Xф, Yф, Hпр, Hф) → схема в DXF для AutoCAD и nanoCAD
        с отклонениями, отметками, проверкой допусков и штампом.</p>
      <Alerts />
      <div className="panel">
        <div className="row">
          <div className="field"><label>Файл съёмки</label><input type="file" onChange={(e) => e.target.files?.[0] && parse(e.target.files[0])} /></div>
          {DEMO && (
            <div className="field" style={{ flex: "0 0 auto" }}>
              <button className="btn" onClick={() => {
                setPoints(demoSurvey());
                setForm({ ...form, title: "Исполнительная схема фундаментной плиты Пм1 в осях 1-5/А-Г", code: "ЖК-01-ИГС-01" });
              }}>Пример съёмки</button>
            </div>
          )}
          <div className="field" style={{ flex: "0 0 auto" }}>
            <label><input type="checkbox" style={{ width: "auto" }} checked={geodetic} onChange={(e) => setGeodetic(e.target.checked)} /> X — на север (геодезическая система)</label>
          </div>
          <Picker label="Объект" value={objectId} onChange={setObjectId} options={objects} />
        </div>
        <div className="row">
          <div className="field"><label>Наименование схемы</label><input value={form.title} onChange={set("title")} /></div>
          <div className="field" style={{ flex: "0 0 200px" }}><label>Шифр</label><input value={form.code} onChange={set("code")} /></div>
        </div>
        <div className="row">
          <div className="field"><label>Поперечные оси (марка:координата, м)</label><input value={form.xAxes} onChange={set("xAxes")} /></div>
          <div className="field"><label>Продольные оси</label><input value={form.yAxes} onChange={set("yAxes")} /></div>
          <div className="field" style={{ flex: "0 0 120px" }}><label>Допуск в плане, мм</label><input value={form.tolPlan} onChange={set("tolPlan")} /></div>
          <div className="field" style={{ flex: "0 0 120px" }}><label>По высоте, мм</label><input value={form.tolHeight} onChange={set("tolHeight")} /></div>
        </div>
        <div className="row">
          <div className="field"><label>Исполнил (геодезист)</label><input value={form.surveyor} onChange={set("surveyor")} /></div>
          <div className="field"><label>Проверил</label><input value={form.checker} onChange={set("checker")} /></div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!points.length || busy} onClick={build}>Построить схему</button>
          </div>
        </div>
        {points.length > 0 && (
          <table>
            <thead><tr><th>Точка</th><th>Xпр</th><th>Yпр</th><th>Xф</th><th>Yф</th><th>Hпр</th><th>Hф</th><th>ΔX, мм</th><th>ΔY, мм</th><th>ΔH, мм</th></tr></thead>
            <tbody>
              {points.map((p) => {
                const d = (a: number | null, b: number | null) => (a === null || b === null ? "—" : Math.round((a - b) * 1000));
                return (
                  <tr key={p.name}><td>{p.name}</td><td>{p.design_x}</td><td>{p.design_y}</td><td>{p.fact_x ?? "—"}</td><td>{p.fact_y ?? "—"}</td>
                    <td>{p.design_z ?? "—"}</td><td>{p.fact_z ?? "—"}</td><td>{d(p.fact_x, p.design_x)}</td><td>{d(p.fact_y, p.design_y)}</td><td>{d(p.fact_z, p.design_z)}</td></tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      {doc && (
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2>{doc.title}</h2>
              {summary && <p className={doc.issues?.length ? "text-bad" : ""}>{summary.conclusion}. Макс. в плане {summary.max_plan_mm} мм, по высоте {summary.max_height_mm ?? "—"} мм.</p>}
            </div>
            <div>
              <button className="btn primary" onClick={() => download(`/documents/${doc.id}/file`, doc.file_name ?? "scheme.dxf")}>Скачать DXF</button>{" "}
              <button className="btn" onClick={() => run(() => download(`/documents/${doc.id}/dwg`, "scheme.dwg"))}>DWG</button>
            </div>
          </div>
          {preview && <img src={preview} alt="Предпросмотр схемы" style={{ width: "100%", border: "1px solid var(--border)", background: "#fff" }} />}
        </div>
      )}
    </>
  );
}

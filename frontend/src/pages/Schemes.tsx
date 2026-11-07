import { useState } from "react";
import { api, DEMO, download, fetchBlob, upload, type DocumentRow } from "../api";
import { demoSurvey } from "../demo/mock";
import { Hint, PageHead, Picker, useAsync, useObjects, VoiceButton } from "../components";

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
  const [mode, setMode] = useState<"draft" | "survey">("draft");
  const [draft, setDraft] = useState({ element: "колонны", designZ: "", measures: "", visual: "", autoTol: true });
  const [cadMsg, setCadMsg] = useState("");
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

  async function buildDraft() {
    const z = draft.designZ.trim() ? Number(draft.designZ.replace(",", ".")) : null;
    const d = await run(() => api<DocumentRow>("/schemes/draft", {
      method: "POST",
      json: {
        title: form.title, code: form.code, element: draft.element, object_id: objectId || null,
        x_axes: parseAxes(form.xAxes), y_axes: parseAxes(form.yAxes), design_z: z, measurements_text: draft.measures,
        visual_note: draft.visual,
        tolerance_plan_mm: draft.autoTol ? null : Number(form.tolPlan), tolerance_height_mm: draft.autoTol ? null : Number(form.tolHeight),
      },
    }));
    if (!d) return;
    setDoc(d);
    const blob = await run(() => fetchBlob(`/documents/${d.id}/preview`));
    if (blob) setPreview(URL.createObjectURL(blob));
  }

  async function openInCad() {
    if (!doc) return;
    setCadMsg("");
    const r = await run(() => api<{ opened_in: string }>(`/documents/${doc.id}/open-in-cad`, { method: "POST" }));
    if (r) setCadMsg(`Открыто в ${r.opened_in === "autocad" ? "AutoCAD" : "nanoCAD"}`);
  }

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const summary = (doc as unknown as { data?: { summary?: { conclusion: string; max_plan_mm: number; max_height_mm: number | null } } })?.data?.summary;

  return (
    <>
      <PageHead title="Исполнительные схемы" hint={<>Схема в DXF для AutoCAD и nanoCAD: оси, контур, отклонения, отметки, проверка допусков, штамп.
        Без геодезиста — по проекту, нормативу и замерам рулеткой или голосом; со съёмкой — по файлу геодезиста.</>} />
      <div className="tabs" style={{ marginBottom: 12, display: "inline-flex" }}>
        <button className={`tab ${mode === "draft" ? "active" : ""}`} onClick={() => setMode("draft")}>По проекту и замерам</button>
        <button className={`tab ${mode === "survey" ? "active" : ""}`} onClick={() => setMode("survey")}>По съёмке геодезиста</button>
      </div>
      <Alerts />
      {mode === "draft" && (
        <div className="panel">
          <div className="row">
            <Picker label="Объект" value={objectId} onChange={setObjectId} options={objects} />
            <div className="field"><label>Наименование схемы</label><input value={form.title} onChange={set("title")} /></div>
            <div className="field" style={{ flex: "0 0 220px" }}><label>Конструкция (для допусков по СП)</label>
              <input value={draft.element} onChange={(e) => setDraft({ ...draft, element: e.target.value })} placeholder="колонны, стены, плита…" /></div>
          </div>
          <div className="row">
            <div className="field"><label>Поперечные оси по проекту (марка:координата, м)</label><input value={form.xAxes} onChange={set("xAxes")} /></div>
            <div className="field"><label>Продольные оси</label><input value={form.yAxes} onChange={set("yAxes")} /></div>
            <div className="field" style={{ flex: "0 0 140px" }}><label>Проектная отметка, м</label>
              <input value={draft.designZ} onChange={(e) => setDraft({ ...draft, designZ: e.target.value })} placeholder="+3,300" /></div>
          </div>
          <div className="field">
            <label>Замеры на месте (рулетка, нивелир) — текстом или голосом</label>
            <div className="row" style={{ alignItems: "flex-start", gap: 8 }}>
              <textarea style={{ flex: 1 }} rows={3} value={draft.measures} onChange={(e) => setDraft({ ...draft, measures: e.target.value })}
                placeholder="1/А: X +8, Y -3, высота -5; 2/Б: по X минус 12, по Y 4. Пусто — схема по проекту с графами для замеров." />
              <VoiceButton onText={(t) => setDraft((d) => ({ ...d, measures: (d.measures ? d.measures + "; " : "") + t }))} />
            </div>
          </div>
          <div className="row">
            <div className="field"><label>Визуальный контроль по фото/видео (необязательно)</label>
              <input value={draft.visual} onChange={(e) => setDraft({ ...draft, visual: e.target.value })} placeholder="армирование выполнено, фиксаторы установлены" /></div>
            <div className="field" style={{ flex: "0 0 auto" }}>
              <label><input type="checkbox" style={{ width: "auto" }} checked={draft.autoTol} onChange={(e) => setDraft({ ...draft, autoTol: e.target.checked })} /> Допуски — из нормативов (ИИ)</label>
            </div>
            {!draft.autoTol && <>
              <div className="field" style={{ flex: "0 0 120px" }}><label>Допуск в плане, мм</label><input value={form.tolPlan} onChange={set("tolPlan")} /></div>
              <div className="field" style={{ flex: "0 0 120px" }}><label>По высоте, мм</label><input value={form.tolHeight} onChange={set("tolHeight")} /></div>
            </>}
            <div className="field" style={{ flex: "0 0 auto" }}>
              <button className="btn primary" disabled={busy} onClick={buildDraft}>Построить схему</button>
              {" "}<Hint>Отклонения в миллиметрах берутся только из замеров — по фото их не измерить; фото и видео
                попадают в примечание как визуальный контроль.</Hint>
            </div>
          </div>
        </div>
      )}
      {mode === "survey" && <div className="panel">
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
      </div>}
      {doc && (
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <h2>{doc.title}</h2>
              {summary && <p className={doc.issues?.length ? "text-bad" : ""}>{summary.conclusion}. Макс. в плане {summary.max_plan_mm} мм, по высоте {summary.max_height_mm ?? "—"} мм.</p>}
            </div>
            <div>
              <button className="btn primary" onClick={() => download(`/documents/${doc.id}/file`, doc.file_name ?? "scheme.dxf")}>Скачать DXF</button>{" "}
              <button className="btn" onClick={() => run(() => download(`/documents/${doc.id}/dwg`, "scheme.dwg"))}>DWG</button>{" "}
              {!DEMO && <button className="btn" onClick={() => void openInCad()} title="BIS и CAD на одном компьютере">Открыть в AutoCAD/nanoCAD</button>}
              {cadMsg && <div className="small" style={{ color: "var(--ok)" }}>{cadMsg}</div>}
            </div>
          </div>
          {preview && <img src={preview} alt="Предпросмотр схемы" style={{ width: "100%", border: "1px solid var(--border)", background: "#fff" }} />}
        </div>
      )}
    </>
  );
}

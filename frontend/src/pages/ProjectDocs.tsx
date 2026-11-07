import { useEffect, useState } from "react";
import { api, upload, type DocumentRow } from "../api";
import { Badge, num, Picker, useAsync, useContracts, useObjects } from "../components";

interface Section { code: string; title: string; required: boolean; present: boolean; documents: string[] }
interface Report { sections: Section[]; missing_required: string[]; rd_sets: Record<string, string[]>; unrecognized: string[]; note: string }
interface CmpRow { spec_name: string; estimate_item: string; code: string | null; unit: string; spec_quantity: number; estimate_quantity: number; difference: number; difference_pct: number | null; status: string }
interface Cmp { rows: CmpRow[]; not_in_estimate: { name: string; unit: string; quantity: number }[] }

export default function ProjectDocs() {
  const objects = useObjects();
  const contracts = useContracts();
  const [objectId, setObjectId] = useState("");
  const [contractId, setContractId] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [docId, setDocId] = useState("");
  const [files, setFiles] = useState<FileList | null>(null);
  const [cmp, setCmp] = useState<Cmp | null>(null);
  const { busy, run, Alerts } = useAsync();

  const load = () => {
    if (!objectId) return;
    api<Report>(`/objects/${objectId}/pd-completeness`).then(setReport);
    api<DocumentRow[]>(`/documents?kind=project_doc&object_id=${objectId}`).then(setDocs);
  };
  useEffect(load, [objectId]);

  async function uploadAll() {
    if (!files) return;
    for (const f of Array.from(files)) {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("kind", "project_doc");
      fd.append("object_id", objectId);
      await run(() => upload("/documents/upload", fd));
    }
    setFiles(null);
    load();
  }

  return (
    <>
      <h1>Проектная документация</h1>
      <Alerts />
      <div className="panel row">
        <Picker label="Объект" value={objectId} onChange={setObjectId} options={objects} />
        <div className="field"><label>Загрузить разделы ПД / комплекты РД (можно несколько)</label>
          <input type="file" multiple onChange={(e) => setFiles(e.target.files)} /></div>
        <div className="field" style={{ flex: "0 0 auto" }}><button className="btn primary" disabled={!files || busy} onClick={uploadAll}>Загрузить</button></div>
      </div>

      {report && (
        <div className="grid2">
          <div className="panel">
            <h2>Комплектность ПД</h2>
            {report.missing_required.length > 0
              ? <div className="alert error">Нет обязательных разделов: {report.missing_required.join("; ")}</div>
              : <div className="alert ok">Все обязательные разделы представлены</div>}
            <table>
              <tbody>
                {report.sections.map((s) => (
                  <tr key={s.code}>
                    <td><b>{s.code}</b></td>
                    <td>{s.title}{!s.required && <span className="small muted"> (при наличии)</span>}</td>
                    <td>{s.present ? <Badge status="ok" /> : s.required ? <Badge status="overdue" /> : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small muted">{report.note}</p>
          </div>
          <div className="panel">
            <h2>Рабочая документация</h2>
            {Object.keys(report.rd_sets).length === 0 && <p className="muted">Комплекты РД не загружены</p>}
            <ul>{Object.entries(report.rd_sets).map(([k, v]) => <li key={k}><b>{k}</b>: {v.join(", ")}</li>)}</ul>
            {report.unrecognized.length > 0 && (<><h2>Не распознаны</h2><ul className="small">{report.unrecognized.map((u) => <li key={u}>{u}</li>)}</ul></>)}
          </div>
        </div>
      )}

      <div className="panel">
        <h2>Сверка спецификации со сметой</h2>
        <div className="row">
          <div className="field">
            <label>Документ проекта</label>
            <select value={docId} onChange={(e) => setDocId(e.target.value)}>
              <option value="">—</option>
              {docs.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}
            </select>
          </div>
          <Picker label="Договор (смета)" value={contractId} onChange={setContractId} options={contracts} />
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!docId || !contractId || busy}
              onClick={() => run(() => api<Cmp>(`/documents/${docId}/compare-estimate`, { method: "POST", json: { contract_id: contractId } })).then((r) => r && setCmp(r))}>
              Сверить
            </button>
          </div>
        </div>
        {cmp && (
          <>
            <table>
              <thead><tr><th>По проекту</th><th>Позиция сметы</th><th>Проект</th><th>Смета</th><th>Разница</th><th /></tr></thead>
              <tbody>
                {cmp.rows.map((r, i) => (
                  <tr key={i}><td>{r.spec_name}</td><td className="small">{r.code} {r.estimate_item}</td>
                    <td>{num(r.spec_quantity, 3)} {r.unit}</td><td>{num(r.estimate_quantity, 3)}</td>
                    <td className={r.status === "mismatch" ? "text-bad" : ""}>{num(r.difference, 3)} ({r.difference_pct ?? "—"}%)</td><td><Badge status={r.status} /></td></tr>
                ))}
              </tbody>
            </table>
            {cmp.not_in_estimate.length > 0 && (
              <div className="alert error">Нет в смете (риск неоплачиваемых работ): {cmp.not_in_estimate.map((m) => `${m.name} — ${num(m.quantity, 3)} ${m.unit}`).join("; ")}</div>
            )}
          </>
        )}
      </div>
    </>
  );
}

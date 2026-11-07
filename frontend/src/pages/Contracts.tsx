import { useEffect, useState } from "react";
import { api, upload } from "../api";
import { Badge, date, Hint, num, Picker, useAsync, useContracts, useObjects } from "../components";

interface Analysis {
  risks?: string[]; penalties?: string[]; advance?: string | null; warranty?: string | null; used_fallback?: boolean;
  provider?: string | null; analyzed_at?: string;
}
interface Contract {
  id: string; number: string; date: string | null; subject: string | null; amount: string | null; kind: string;
  our_role: string; end_date: string | null; document_id: string | null; extra: { analysis?: Analysis };
}
interface Obligation {
  id: string; description: string; party: string; kind: string; due_date: string | null; due_text: string | null;
  status: string; penalty: string | null; source_clause: string | null;
}

const KINDS: Record<string, string> = { general: "Генподряд", subcontract: "Субподряд", supply: "Поставка", services: "Услуги", design: "Проектирование" };

export default function Contracts() {
  const contracts = useContracts();
  const objects = useObjects();
  const [id, setId] = useState("");
  const [c, setC] = useState<Contract | null>(null);
  const [obligations, setObligations] = useState<Obligation[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [newFile, setNewFile] = useState<File | null>(null);
  const [newKind, setNewKind] = useState("subcontract");
  const [newRole, setNewRole] = useState("customer");
  const [newObject, setNewObject] = useState("");
  const [penalty, setPenalty] = useState({ base_amount: "", days: "", rate_percent_per_day: "0.1", cap_percent: "" });
  const [penaltyResult, setPenaltyResult] = useState<{ penalty: number; formula: string } | null>(null);
  const { busy, run, Alerts } = useAsync();

  const load = () => {
    if (!id) return;
    api<Contract>(`/contracts/${id}`).then(setC);
    api<Obligation[]>(`/obligations?filter_field=contract_id&filter_value=${id}&limit=500`).then((rows) =>
      setObligations(rows.sort((a, b) => (a.due_date ?? "9999").localeCompare(b.due_date ?? "9999"))));
  };
  useEffect(load, [id]);

  async function attach() {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await run(() => upload(`/contracts/${id}/file`, form), "Договор загружен и проанализирован");
    setFile(null);
    load();
  }

  async function importNew() {
    if (!newFile) return;
    const form = new FormData();
    form.append("file", newFile);
    form.append("kind", newKind);
    form.append("our_role", newRole);
    if (newObject) form.append("object_id", newObject);
    const r = await run(() => upload<{ contract_id: string; number: string; obligations_added: number }>("/contracts/import", form));
    if (r) {
      setNewFile(null);
      window.location.reload();
    }
  }

  const a = c?.extra?.analysis;
  return (
    <>
      <h1>Договоры</h1>
      <Alerts />
      <div className="panel">
        <div className="row">
          <Picker label="Договор" value={id} onChange={setId} options={contracts} />
        </div>
        {c && (
          <div className="small">
            {KINDS[c.kind] ?? c.kind} · {c.our_role === "contractor" ? "мы — подрядчик/исполнитель" : "мы — заказчик"} · от {date(c.date)} ·
            цена {num(c.amount)} руб. · окончание {date(c.end_date)}
            <div className="muted">{c.subject}</div>
          </div>
        )}
      </div>

      {c && (
        <div className="grid2">
          <div className="panel">
            <h2>Анализ договора</h2>
            {!a && <p className="muted">Договор ещё не анализировался.</p>}
            {a && (
              <>
                <p className="small muted">
                  {a.used_fallback ? "Разобран правилами (модель недоступна)" : `Разобран моделью ${a.provider ?? ""}`} ·{" "}
                  {a.analyzed_at ? new Date(a.analyzed_at).toLocaleString("ru-RU") : ""}
                </p>
                {a.advance && <p><b>Аванс:</b> {a.advance}</p>}
                {a.warranty && <p><b>Гарантия:</b> {a.warranty}</p>}
                <b>Риски</b>
                <ul className="issues">
                  {(a.risks ?? []).map((r, i) => <li key={i} className="warning">{r}</li>)}
                  {!a.risks?.length && <li className="muted">не выявлены</li>}
                </ul>
                <b>Неустойки</b>
                <ul className="small">{(a.penalties ?? []).map((p, i) => <li key={i}>{p}</li>)}</ul>
              </>
            )}
            <div className="row">
              <div className="field">
                <label>{c.document_id ? "Заменить файл договора" : "Файл договора (PDF, DOCX, TXT)"}</label>
                <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
              </div>
              <div className="field" style={{ flex: "0 0 auto" }}>
                <button className="btn primary" disabled={!file || busy} onClick={attach}>Загрузить и проанализировать</button>
              </div>
              {c.document_id && (
                <div className="field" style={{ flex: "0 0 auto" }}>
                  <button className="btn" disabled={busy} onClick={() => run(() => api(`/contracts/${id}/analyze`, { method: "POST" }), "Анализ обновлён").then(load)}>
                    Проанализировать заново
                  </button>
                </div>
              )}
            </div>
          </div>

          <div className="panel">
            <h2>Расчёт неустойки</h2>
            <div className="row">
              {([["base_amount", "Сумма, руб."], ["days", "Дней просрочки"], ["rate_percent_per_day", "% в день"], ["cap_percent", "Ограничение, %"]] as const).map(([k, l]) => (
                <div className="field" key={k}>
                  <label>{l}</label>
                  <input value={penalty[k]} onChange={(e) => setPenalty({ ...penalty, [k]: e.target.value })} />
                </div>
              ))}
            </div>
            <button className="btn" onClick={() => run(() => api<{ penalty: number; formula: string }>("/contracts/penalty", {
              method: "POST",
              json: { base_amount: Number(penalty.base_amount), days: Number(penalty.days),
                      rate_percent_per_day: Number(penalty.rate_percent_per_day), cap_percent: penalty.cap_percent ? Number(penalty.cap_percent) : null },
            })).then((r) => r && setPenaltyResult(r))}>Рассчитать</button>
            {penaltyResult && <p><b>{num(penaltyResult.penalty)} руб.</b> <span className="small muted">({penaltyResult.formula})</span></p>}
          </div>
        </div>
      )}

      {c && (
        <div className="panel">
          <h2>Обязательства ({obligations.length})</h2>
          <table>
            <thead><tr><th>Пункт</th><th>Обязательство</th><th>Сторона</th><th>Срок</th><th>Статус</th></tr></thead>
            <tbody>
              {obligations.map((o) => (
                <tr key={o.id}>
                  <td className="small">{o.source_clause}</td>
                  <td>{o.description}{o.penalty && <div className="small muted">{o.penalty.slice(0, 200)}</div>}</td>
                  <td>{o.party === "we" ? "мы" : "контрагент"}</td>
                  <td>{o.due_date ? date(o.due_date) : <span className="small muted">{o.due_text ?? "—"}</span>}</td>
                  <td><Badge status={o.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <h2>Новый договор из файла <Hint>Номер, сумма, сроки, контрагент и обязательства извлекаются автоматически.</Hint></h2>
        <div className="row">
          <div className="field"><input type="file" onChange={(e) => setNewFile(e.target.files?.[0] ?? null)} /></div>
          <div className="field">
            <select value={newKind} onChange={(e) => setNewKind(e.target.value)}>
              {Object.entries(KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </div>
          <div className="field">
            <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
              <option value="customer">Мы — заказчик / покупатель</option>
              <option value="contractor">Мы — подрядчик / исполнитель</option>
            </select>
          </div>
          <Picker label="" value={newObject} onChange={setNewObject} options={objects} empty="Объект не указан" />
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!newFile || busy} onClick={importNew}>Импортировать</button>
          </div>
        </div>
      </div>
    </>
  );
}

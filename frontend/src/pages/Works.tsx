import { useEffect, useState } from "react";
import { api, download, upload, type DocumentRow, type Issue } from "../api";
import { Badge, date, Issues, num, Picker, useAsync, useContracts, useObjects } from "../components";

interface PFRow {
  task_id: string; name: string; unit: string | null; planned_start: string; planned_end: string; planned_quantity: string;
  planned_to_date: string; actual: string; percent_fact: number; percent_plan: number; status: string; forecast_end: string | null;
}
interface ClosingRow { code: string | null; name: string; unit: string; include_quantity: number; unit_price: number; amount: number }
interface Closing { rows: ClosingRow[]; issues: Issue[]; total: number; vat: number; total_with_vat: number }
interface M29Row { material: string; unit: string; norm_total: number; actual: number | null; deviation: number | null; deviation_percent: number | null; status: string }
interface Balance { material: string; unit: string; received: number; used: number; balance: number; problem: string | null }

const monthStart = () => { const d = new Date(); d.setDate(1); return d.toISOString().slice(0, 10); };
const monthEnd = () => { const d = new Date(); d.setMonth(d.getMonth() + 1, 0); return d.toISOString().slice(0, 10); };

export default function Works() {
  const objects = useObjects();
  const contracts = useContracts();
  const [objectId, setObjectId] = useState("");
  const [contractId, setContractId] = useState("");
  const [start, setStart] = useState(monthStart());
  const [end, setEnd] = useState(monthEnd());
  const [pf, setPf] = useState<PFRow[]>([]);
  const [closing, setClosing] = useState<Closing | null>(null);
  const [m29, setM29] = useState<{ rows: M29Row[]; issues: Issue[] } | null>(null);
  const [balance, setBalance] = useState<Balance[]>([]);
  const [created, setCreated] = useState<DocumentRow[]>([]);
  const [estimateFile, setEstimateFile] = useState<File | null>(null);
  const { busy, run, Alerts } = useAsync();
  const period = { period_start: start, period_end: end };

  useEffect(() => {
    if (!objectId) return;
    api<{ rows: PFRow[] }>(`/objects/${objectId}/plan-fact`).then((r) => setPf(r.rows));
    api<Balance[]>(`/objects/${objectId}/materials/balance`).then(setBalance);
  }, [objectId]);

  const checkClosing = () => run(() => api<Closing>(`/contracts/${contractId}/closing/check`, { method: "POST", json: period })).then((r) => r && setClosing(r));
  const ks2 = () => run(() => api<{ ks2: DocumentRow; ks3: DocumentRow }>(`/contracts/${contractId}/closing/ks2`, { method: "POST", json: period }),
    "КС-2 и КС-3 сформированы — утвердите в разделе «Документы»").then((r) => { if (r) { setCreated([r.ks2, r.ks3]); checkClosing(); } });
  const checkM29 = () => run(() => api<{ rows: M29Row[]; issues: Issue[] }>("/materials/m29/preview", { method: "POST", json: { object_id: objectId, ...period } })).then((r) => r && setM29(r));
  const makeDoc = (path: string, msg: string) => run(() => api<DocumentRow>(path, { method: "POST", json: { object_id: objectId, ...period } }), msg)
    .then((d) => d && setCreated([d]));

  async function importEstimate() {
    if (!estimateFile) return;
    const form = new FormData();
    form.append("file", estimateFile);
    await run(() => upload<{ imported: number; hidden_works: number; total: number }>(`/contracts/${contractId}/estimate/import`, form))
      .then((r) => r && alert(`Импортировано позиций: ${r.imported} (скрытых работ: ${r.hidden_works}), сумма ${num(r.total)} руб.`));
  }

  return (
    <>
      <h1>Работы, материалы, закрытие</h1>
      <Alerts />
      <div className="panel row">
        <Picker label="Объект" value={objectId} onChange={setObjectId} options={objects} />
        <Picker label="Договор" value={contractId} onChange={setContractId} options={contracts} />
        <div className="field" style={{ flex: "0 0 160px" }}><label>Период с</label><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></div>
        <div className="field" style={{ flex: "0 0 160px" }}><label>по</label><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
      </div>

      {created.length > 0 && (
        <div className="alert ok">
          Создано: {created.map((d) => (
            <span key={d.id}> <a href="#" onClick={(e) => { e.preventDefault(); download(`/documents/${d.id}/file`, d.file_name ?? undefined); }}>{d.title}</a></span>
          ))}
          <Issues items={created.flatMap((d) => d.issues ?? [])} />
        </div>
      )}

      <div className="panel">
        <h2>План-факт по ГПР</h2>
        <table>
          <thead><tr><th>Работа</th><th>План</th><th>По плану на сегодня</th><th>Факт</th><th>%</th><th>Прогноз окончания</th><th>Статус</th></tr></thead>
          <tbody>
            {pf.length === 0 && <tr><td colSpan={7} className="muted">График не заполнен (раздел «ГПР»)</td></tr>}
            {pf.map((r) => (
              <tr key={r.task_id}>
                <td>{r.name}<div className="small muted">{date(r.planned_start)} — {date(r.planned_end)}</div></td>
                <td>{num(r.planned_quantity)} {r.unit}</td>
                <td>{num(r.planned_to_date)}</td>
                <td>{num(r.actual)}</td>
                <td><div className="bar"><div className="bar-plan" style={{ width: `${Math.min(r.percent_plan, 100)}%` }} /><div className={`bar-fact t-${r.status}`} style={{ width: `${Math.min(r.percent_fact, 100)}%` }} /></div>{r.percent_fact}%</td>
                <td className={r.forecast_end && r.forecast_end > r.planned_end ? "text-bad" : ""}>{date(r.forecast_end)}</td>
                <td><Badge status={r.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>Закрытие: КС-2 / КС-3</h2>
          <div>
            <button className="btn" disabled={!contractId || busy} onClick={checkClosing}>Проверить</button>{" "}
            <button className="btn primary" disabled={!closing?.rows.length || busy} onClick={ks2}>Сформировать КС-2 и КС-3</button>
          </div>
        </div>
        {closing && (
          <>
            <table>
              <thead><tr><th>Поз.</th><th>Работа</th><th>Объём</th><th>Цена</th><th>Сумма</th></tr></thead>
              <tbody>
                {closing.rows.map((r, i) => (
                  <tr key={i}><td>{r.code}</td><td>{r.name}</td><td>{num(r.include_quantity, 4)} {r.unit}</td><td>{num(r.unit_price)}</td><td>{num(r.amount)}</td></tr>
                ))}
                <tr><td colSpan={4} style={{ textAlign: "right" }}><b>Итого / НДС / Всего</b></td><td><b>{num(closing.total)} / {num(closing.vat)} / {num(closing.total_with_vat)}</b></td></tr>
              </tbody>
            </table>
            <Issues items={closing.issues} />
          </>
        )}
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2>Расход материалов (М-29)</h2>
            <div>
              <button className="btn" disabled={!objectId || busy} onClick={checkM29}>Рассчитать</button>{" "}
              <button className="btn" disabled={!m29 || busy} onClick={() => makeDoc("/materials/m29", "М-29 сформирован")}>М-29</button>{" "}
              <button className="btn" disabled={!m29 || busy} onClick={() => makeDoc("/materials/write-off", "Акт списания сформирован")}>Акт списания</button>
            </div>
          </div>
          {m29 && (
            <>
              <table>
                <thead><tr><th>Материал</th><th>По норме</th><th>Факт</th><th>Откл.</th><th /></tr></thead>
                <tbody>
                  {m29.rows.map((r, i) => (
                    <tr key={i}><td>{r.material}, {r.unit}</td><td>{num(r.norm_total, 3)}</td><td>{num(r.actual, 3)}</td>
                      <td>{r.deviation !== null ? `${num(r.deviation, 3)} (${r.deviation_percent ?? "—"}%)` : "—"}</td><td><Badge status={r.status} /></td></tr>
                  ))}
                </tbody>
              </table>
              <Issues items={m29.issues} />
            </>
          )}
        </div>
        <div className="panel">
          <h2>Остатки материалов на объекте</h2>
          <table>
            <thead><tr><th>Материал</th><th>Приход</th><th>Расход</th><th>Остаток</th></tr></thead>
            <tbody>
              {balance.map((b, i) => (
                <tr key={i}><td>{b.material}, {b.unit}{b.problem && <div className="small text-bad">{b.problem}</div>}</td>
                  <td>{num(b.received, 3)}</td><td>{num(b.used, 3)}</td><td className={b.balance < 0 ? "text-bad" : ""}>{num(b.balance, 3)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h2>Импорт сметы / ведомости объёмов (Excel)</h2>
        <p className="muted small">Заголовки (№, шифр, наименование, ед. изм., количество, цена) и разделы определяются автоматически; скрытые работы помечаются для контроля АОСР.</p>
        <div className="row">
          <div className="field"><input type="file" accept=".xlsx" onChange={(e) => setEstimateFile(e.target.files?.[0] ?? null)} /></div>
          <div className="field" style={{ flex: "0 0 auto" }}><button className="btn primary" disabled={!estimateFile || !contractId || busy} onClick={importEstimate}>Импортировать в договор</button></div>
        </div>
      </div>
    </>
  );
}

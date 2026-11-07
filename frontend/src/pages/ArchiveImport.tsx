// Загрузка архива организации: папки с ИД, договорами, письмами, проектом. Тип документа определяет ИИ.
import { useRef, useState } from "react";
import { api } from "../api";
import { Hint, PageHead, useObjects } from "../components";

interface IdRef { kind: string; title: string; number: string; found: boolean; document_title: string | null }
interface IdAct { document_id: string; title: string; date: string | null; works: string; refs: IdRef[]; issues: string[]; complete: boolean }
interface IdCheck { acts: number; complete: number; missing_refs: number; with_issues: number; items: IdAct[] }

interface Result {
  file: string; status: string; type?: string; kind?: string; document_id?: string; object_id?: string | null;
  text?: string; obligations?: number; error?: string; reason?: string;
}

const TYPE_RU: Record<string, string> = {
  aosr: "АОСР", aook: "АООК", scheme: "Исполнительная схема", ks2: "КС-2", ks3: "КС-3", order: "Приказ",
  work_journal: "Журнал работ", contract: "Договор", letter: "Письмо", project_doc: "Проект/РД", upd: "Накладная",
  certificate: "Документ о качестве", test_protocol: "Протокол испытаний", survey: "Съёмка", site_photo: "Фото",
  foreman_report: "Прочее", unknown: "Прочее",
};
const TEXT_RU: Record<string, string> = { parsed: "текст", ocr: "распознан скан", dxf: "надписи чертежа", none: "без текста" };

export default function ArchiveImport() {
  const objects = useObjects();
  const [objectId, setObjectId] = useState("");
  const [contracts, setContracts] = useState(true);
  const [results, setResults] = useState<Result[]>([]);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [drag, setDrag] = useState(false);
  const stop = useRef(false);
  const files = useRef<HTMLInputElement>(null);
  const folder = useRef<HTMLInputElement>(null);

  async function run(list: File[]) {
    const items = list.filter((f) => f.size > 0);
    if (!items.length) return;
    stop.current = false;
    setResults([]);
    setProgress({ done: 0, total: items.length });
    let next = 0;
    let done = 0;
    const one = async (f: File) => {
      const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || "";
      const form = new FormData();
      form.append("file", f, f.name);
      if (objectId) form.append("object_id", objectId);
      form.append("folder", rel.split("/").slice(0, -1).join("/"));
      form.append("analyze_contracts", String(contracts));
      let r: Result;
      try {
        r = await api<Result>("/archive/import", { method: "POST", body: form });
      } catch (e) {
        r = { file: f.name, status: "error", error: (e as Error).message };
      }
      setResults((x) => [r, ...x]);
      done += 1;
      setProgress({ done, total: items.length });
    };
    // по 3 файла одновременно: распознавание, модели и векторы — сетевые запросы, ждать их по очереди незачем
    const lane = async () => {
      while (!stop.current && next < items.length) await one(items[next++]);
    };
    await Promise.all([lane(), lane(), lane()]);
    setCheck(null);
  }

  const [check, setCheck] = useState<IdCheck | null>(null);
  const [checking, setChecking] = useState(false);
  async function runCheck() {
    setChecking(true);
    try {
      setCheck(await api<IdCheck>(`/archive/id-check${objectId ? `?object_id=${objectId}` : ""}`));
    } finally {
      setChecking(false);
    }
  }

  const counts = results.reduce<Record<string, number>>((acc, r) => {
    const k = r.status === "ok" ? TYPE_RU[r.type ?? ""] ?? "Прочее" : r.status === "duplicate" ? "Повторы" : r.status === "error" ? "Ошибки" : "Пропущено";
    acc[k] = (acc[k] ?? 0) + 1;
    return acc;
  }, {});
  const running = progress && progress.done < progress.total && !stop.current;

  return (
    <>
      <PageHead title="Загрузка архива" hint={<>Исполнительная документация (АОСР, АООК, схемы), договоры, письма, приказы, проект, КС-2/КС-3, сертификаты — целыми
        папками. ИИ определит тип каждого файла, разложит в реестр объекта и добавит в базу знаний: по ним работает умный поиск,
        а ИИ-специалисты берут их как образцы. Договоры встанут на контроль; письма из архива регистрируются без поручений.
        Повторно загруженные файлы пропускаются.</>} />
      <div className={`panel composer-panel ${drag ? "drag" : ""}`}
           onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
           onDrop={(e) => { e.preventDefault(); setDrag(false); void run(Array.from(e.dataTransfer.files)); }}>
        <div className="row">
          <div className="field">
            <label>Объект</label>
            <select value={objectId} onChange={(e) => setObjectId(e.target.value)}>
              <option value="">Определить автоматически (по папке и тексту)</option>
              {objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <label><input type="checkbox" style={{ width: "auto" }} checked={contracts} onChange={(e) => setContracts(e.target.checked)} /> Договоры — разобрать на обязательства и поставить на контроль</label>
          </div>
        </div>
        <input ref={files} type="file" multiple hidden onChange={(e) => e.target.files && void run(Array.from(e.target.files))} />
        {/* @ts-expect-error — выбор папки целиком (Chrome, Edge) */}
        <input ref={folder} type="file" webkitdirectory="" hidden onChange={(e) => e.target.files && void run(Array.from(e.target.files))} />
        <div className="row" style={{ alignItems: "center" }}>
          <button className="btn primary" disabled={!!running} onClick={() => folder.current?.click()}>📁 Выбрать папку</button>
          <button className="btn" disabled={!!running} onClick={() => files.current?.click()}>📄 Выбрать файлы</button>
          <span className="muted small">или перетащите файлы сюда · PDF, DOCX, XLSX, DWG, DXF, сканы, EML, TXT</span>
          {running && <button className="btn small" onClick={() => { stop.current = true; }}>Остановить</button>}
        </div>
        {progress && (
          <div style={{ marginTop: 12 }}>
            <div className="bar" style={{ width: "100%", height: 8 }}>
              <div className="bar-fact" style={{ width: `${(progress.done / progress.total) * 100}%`, height: 8 }} />
            </div>
            <div className="small muted" style={{ marginTop: 4 }}>
              {progress.done} из {progress.total}{progress.done === progress.total ? " — готово" : " — ИИ разбирает…"}
            </div>
          </div>
        )}
      </div>
      <div className="panel">
        <div className="row" style={{ alignItems: "center", justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>Проверка ИД по архиву <Hint>Для каждого АОСР и АООК: есть ли в архиве документы о качестве, протоколы
            испытаний и исполнительные схемы, на которые он ссылается по номеру; указаны ли схема, шифр РД и даты.</Hint></h2>
          <button className="btn" disabled={checking || !!running} onClick={() => void runCheck()}>{checking ? "Проверяю…" : "Проверить"}</button>
        </div>
        {check && (
          <>
            <div className="stats" style={{ marginTop: 12 }}>
              <div className="stat"><div className="stat-value">{check.acts}</div><div className="stat-label">актов</div></div>
              <div className="stat ok"><div className="stat-value">{check.complete}</div><div className="stat-label">полных</div></div>
              <div className={`stat ${check.missing_refs ? "bad" : ""}`}><div className="stat-value">{check.missing_refs}</div><div className="stat-label">нет в архиве</div></div>
              <div className={`stat ${check.with_issues ? "warn" : ""}`}><div className="stat-value">{check.with_issues}</div><div className="stat-label">с замечаниями</div></div>
            </div>
            {check.items.filter((a) => !a.complete).map((a) => (
              <div key={a.document_id} className="id-act">
                <b>{a.title}</b>{a.date && <span className="muted small"> · {new Date(a.date).toLocaleDateString("ru-RU")}</span>}
                <ul>
                  {a.refs.filter((r) => !r.found).map((r) => <li key={r.kind + r.number} className="text-bad">нет в архиве: {r.title} № {r.number}</li>)}
                  {a.issues.map((x) => <li key={x} className="text-warn">{x}</li>)}
                </ul>
              </div>
            ))}
            {check.acts > 0 && check.complete === check.acts && <p className="muted">Все акты полные: ссылки найдены в архиве.</p>}
            {check.acts === 0 && <p className="muted">АОСР и АООК в архиве нет.</p>}
          </>
        )}
      </div>
      {results.length > 0 && (
        <>
          <div className="stats">
            {Object.entries(counts).map(([k, v]) => <div key={k} className="stat"><div className="stat-value">{v}</div><div className="stat-label">{k}</div></div>)}
          </div>
          <div className="panel">
            <table>
              <thead><tr><th>Файл</th><th>Тип</th><th>Объект</th><th>Текст</th><th>Итог</th></tr></thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={i}>
                    <td>{r.file}</td>
                    <td>{r.status === "ok" ? TYPE_RU[r.type ?? ""] ?? r.type : "—"}</td>
                    <td className="small">{r.object_id ? objects.find((o) => o.id === r.object_id)?.label ?? "да" : "—"}</td>
                    <td className="small muted">{r.text ? TEXT_RU[r.text] ?? r.text : ""}</td>
                    <td className="small">
                      {r.status === "ok" && (r.obligations ? `договор: обязательств ${r.obligations}` : "в реестре и базе знаний")}
                      {r.status === "duplicate" && <span className="muted">уже загружен</span>}
                      {r.status === "skipped" && <span className="muted">{r.reason}</span>}
                      {r.status === "error" && <span className="text-bad">{r.error}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

import { useEffect, useRef, useState } from "react";
import { activeProject, api, openFile, type User } from "../api";
import { PageHead } from "../components";
import Icon from "../components/Icon";

type Coverage = { total: number; processed: number; remaining: number; unread: string[]; review: string[]; scope: string };
type Source = { id: string; title: string; file_name: string; reading_id: string | null; state: string; coverage: Coverage | null };
type Claim = { field: string; value: string; quote: string };
type Item = { position: string | null; name: string; mark: string | null; unit: string | null; quantity: string | null; location: string | null; element: string | null; quote: string; header_quote?: string | null };
type Sheet = { ordinal: number; physical_page: number | null; label: string; origin: string; method: string; complete: boolean; claims: Claim[]; items: Item[]; observations: { field?: string; value?: string; name?: string; mark?: string; quote?: string }[]; warnings: string[]; rejected: number };
type Reading = { id: string; state: string; coverage: Coverage; pages: Sheet[] };
const LABELS: Record<string, string> = { code: "Шифр", sheet: "Лист", revision: "Изменение", title: "Наименование", stage: "Стадия", location: "Место", element: "Элемент / линия", axes: "Оси", level: "Отметка", reference: "Ссылка", material: "Материал", work: "Работа", note: "Примечание" };

export default function ProjectReading({ user }: { user: User }) {
  const [docs, setDocs] = useState<Source[]>([]);
  const [selected, setSelected] = useState("");
  const [reading, setReading] = useState<Reading | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [vision, setVision] = useState(false);
  const generation = useRef(0);
  const project = activeProject();
  useEffect(() => {
    let live = true;
    if (project) void api<Source[]>("/projects/documents").then((rows) => { if (live) setDocs(rows); }).catch((e) => { if (live) setError(e.message); });
    return () => { live = false; generation.current += 1; };
  }, [project]);
  async function select(doc: Source) {
    const request = ++generation.current;
    setSelected(doc.id); setReading(null); setError(""); setBusy(false);
    if (doc.reading_id) {
      try { const value = await api<Reading>(`/projects/readings/${doc.reading_id}`); if (request === generation.current) setReading(value); }
      catch (e) { if (request === generation.current) setError((e as Error).message); }
    }
  }
  async function read(force = false) {
    const request = ++generation.current;
    setBusy(true); setError("");
    try {
      const value = await api<Reading>(`/projects/documents/${selected}/read`, { method: "POST", json: { budget: 4, with_vision: vision, force } });
      if (request !== generation.current) return;
      setReading(value);
      const rows = await api<Source[]>("/projects/documents");
      if (request === generation.current) setDocs(rows);
    } catch (e) { if (request === generation.current) setError((e as Error).message); }
    finally { if (request === generation.current) setBusy(false); }
  }
  return <div className="project-reading-workspace">
    <PageHead title="Чтение проекта" hint="Штампы, листы, позиции спецификаций и привязки к элементам. Подтверждённые надписи используются при сверке актов; визуальные предположения требуют проверки." />
    {!project ? <div className="panel"><h2>Выберите проект</h2><p className="muted">Используйте выбор объекта в верхней панели.</p></div> : <>
      {error && <div className="alert error" role="alert">{error}</div>}
      <div className="panel"><div className="field"><label htmlFor="project-source">Проектный документ</label><select id="project-source" value={selected} onChange={(e) => { const doc = docs.find((d) => d.id === e.target.value); if (doc) void select(doc); }}><option value="">Выберите из архива проекта</option>{docs.map((d) => <option value={d.id} key={d.id}>{d.title} · {d.coverage ? `${d.coverage.processed}/${d.coverage.total}` : "ещё не разобран"}</option>)}</select></div>
        {docs.length === 0 && <p className="muted">В этом проекте пока нет РД. Загрузите документ с видом «Проектная документация».</p>}
        {selected && user.role !== "viewer" && <><div className="row"><button className="btn primary" disabled={busy} onClick={() => void read()}><Icon name="project" size={16} />{busy ? "Читаю страницы…" : reading?.coverage.remaining ? "Продолжить чтение · 4 страницы" : "Прочитать проект"}</button>
          {reading && <button className="btn" disabled={busy} onClick={() => void read(true)}>Перечитать заново</button>}
          <label className="small"><input type="checkbox" checked={vision} onChange={(e) => setVision(e.target.checked)} /> Подключить настроенную модель зрения</label></div>
          <p className="muted small">PDF читается с сохранением расположения текста, таблицы — по заголовкам столбцов. Для сканов используются доступный OCR и, при включении, общий вид, штамп и фрагменты листа. Смена результата чтения потребует повторной сверки зависимых актов.</p></>}
      </div>
      {reading && <><div className="panel"><div className="pto-section-head"><h2>{reading.state === "ready" ? "Надписи и таблицы разобраны" : "Чтение неполное"}</h2><span className="badge">{reading.coverage.processed} / {reading.coverage.total}</span></div><p className="muted">{reading.coverage.scope}</p>
        {reading.coverage.remaining > 0 && <p>Осталось страниц или разделов: {reading.coverage.remaining}.</p>}
        {reading.coverage.unread.length > 0 && <div className="note warning">Не прочитаны: {reading.coverage.unread.join(", ")}. Отсутствие распознавания не означает отсутствие данных.</div>}
        {reading.coverage.review.length > 0 && <div className="note warning">Нужна проверка: {reading.coverage.review.join(", ")}.</div>}
      </div>
      {reading.pages.map((page) => <section className="panel project-sheet" key={page.ordinal}>
        <div className="pto-section-head"><h2>{page.label}{page.claims.find((c) => c.field === "sheet") ? ` · лист ${page.claims.find((c) => c.field === "sheet")!.value}` : ""}</h2><button className="btn small" onClick={() => void openFile(`/projects/readings/${reading.id}/file`, docs.find((d) => d.id === selected)?.title ?? "Проект", page.physical_page ?? undefined)}>Оригинал этой версии</button></div>
        <p className="small muted">Чтение: {{ pdf_layout: "текст PDF с расположением строк", xlsx_table: "таблица Excel", native_text: "текст документа", indexed_text: "распознанный текст", image: "изображение", ocr: "OCR", ocr_unavailable: "OCR недоступен" }[page.origin] ?? "исходный документ"}. {page.complete ? "Извлечённые сведения подтверждены текстом." : "Есть непроверенные сведения или непрочитанные области."}</p>
        <div className="project-claims">{page.claims.filter((c) => c.field !== "material").map((c, i) => <details key={i}><summary><span className="small muted">{LABELS[c.field] ?? c.field}</span> {c.value}</summary><blockquote>{c.quote}</blockquote></details>)}</div>
        {page.items.length > 0 && <div className="table-scroll"><table><thead><tr><th>Поз.</th><th>Наименование / марка</th><th>Ед.</th><th>Количество</th><th>Место / элемент</th><th>Основание</th></tr></thead><tbody>{page.items.map((item, i) => <tr key={i}><td>{item.position ?? "—"}</td><td>{item.name}{item.mark && <div className="muted">{item.mark}</div>}</td><td>{item.unit ?? "—"}</td><td>{item.quantity ?? "Не указано"}</td><td>{[item.location, item.element].filter(Boolean).join(" · ") || "Отдельно не указано"}</td><td><details><summary>Цитата</summary>{item.header_quote && <p className="small muted">{item.header_quote}</p>}<blockquote>{item.quote}</blockquote></details></td></tr>)}</tbody></table></div>}
        {(page.observations.length > 0 || page.rejected > 0) && <div className="note warning">Не использовано для автоматического заполнения: {page.rejected} неподтверждённых значений. {page.observations.length > 0 && <details><summary>Визуальные наблюдения для ручной проверки</summary><ul>{page.observations.map((o, i) => <li key={i}>{o.value ?? [o.name, o.mark].filter(Boolean).join(" ")}<blockquote>{o.quote ?? ""}</blockquote></li>)}</ul></details>}</div>}
        {page.warnings.map((w, i) => <p key={i} className="small text-warn">{w}</p>)}
      </section>)}
      </>}
    </>}
  </div>;
}

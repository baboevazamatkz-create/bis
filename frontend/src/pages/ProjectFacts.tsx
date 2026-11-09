import { useEffect, useState } from "react";
import { activeProject, api, openFile, type User } from "../api";
import { PageHead } from "../components";
import Icon from "../components/Icon";

type Fact = { id: string; snapshot_id: string; predicate: string; label: string; category: string; value: string; location: string | null; location_key: string; work_key: string; quote: string; page: number | null; source_title: string; document_id: string | null; report_id: string | null; work_volume_id: string | null; source_sha256: string; method: string; state: string };
type Conflict = { id: string; kind: string; predicate: string; message: string; state: string; revision: number; facts: Fact[]; selected_fact_id: string | null; reason: string | null };
type Coverage = { documents: number; indexed_documents: number; unread: string[]; partial: string[]; stale: string[] };
type Registry = { items: Fact[]; labels: Record<string, string>; truncated: boolean; coverage: Coverage };
type Investigation = { id: string; state: string; text: string; plan: { steps: string[] }; facts: Fact[]; coverage: { read: number; candidates: number; remaining: number; cached: number }; conflicts: Conflict[] };
type History = { id: string; question: string; state: string; created_at: string };
const CATEGORIES: Record<string, string> = { actual: "Фактические данные", planned: "Предусмотрено проектом", reference: "Справочные сведения", requirement: "Требование", result: "Результат испытаний" };
const STATES: Record<string, string> = { ready: "Основания найдены", partial: "Проверка неполная", conflict: "Есть противоречия", needs_data: "Нет подтверждения", ambiguous: "Есть несколько вариантов", running: "Идёт проверка", unsupported: "Другой тип вопроса", open: "Требует сверки", resolved: "Выбор сохранён", stale: "Основания изменились" };
function value(f: Fact) { return /^(work_start|work_end|work_date|certificate_expiry)$/.test(f.predicate) && /^\d{4}-\d\d-\d\d$/.test(f.value) ? f.value.split("-").reverse().join(".") : f.value; }
function Evidence({ fact }: { fact: Fact }) {
  return <details className="fact-evidence"><summary>Основание · {fact.source_title}{fact.page ? `, стр. ${fact.page}` : ""}</summary><blockquote>{fact.quote}</blockquote>
    <div className="fact-proof-actions">{fact.document_id && <button className="btn small" onClick={() => void openFile(`/brain/facts/snapshots/${fact.snapshot_id}/file`, fact.source_title, fact.page ?? undefined)}>Оригинал этой версии <Icon name="arrow" size={13} /></button>}
      <span className="small muted">{fact.report_id ? "Сообщение о выполненной работе" : fact.work_volume_id ? "Учтённая запись работ" : "Извлечено из документа"}</span></div>
    <details className="fact-fingerprint"><summary>Версия основания</summary><code>{fact.source_sha256}</code></details>
  </details>;
}
export default function ProjectFacts({ user }: { user: User }) {
  const project = activeProject();
  const [registry, setRegistry] = useState<Registry>();
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [history, setHistory] = useState<History[]>([]);
  const [tab, setTab] = useState("facts");
  const [filters, setFilters] = useState({ predicate: "", category: "", location: "" });
  const [applied, setApplied] = useState(filters);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<Investigation>();
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const canWrite = user.role !== "viewer";
  function load() {
    const params = new URLSearchParams(Object.entries(applied).filter(([, v]) => !!v));
    return Promise.all([api<Registry>(`/brain/facts?${params}`).then(setRegistry), api<Conflict[]>("/brain/facts/conflicts").then(setConflicts), api<History[]>("/brain/facts/investigations").then(setHistory)]);
  }
  useEffect(() => { if (project) void load().catch((e) => setError(e.message)); }, [project, applied]);
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const open = conflicts.filter((c) => c.state === "open");
  return <div className="fact-workspace"><PageHead title="Факты проекта" hint="Каждое значение связано с исходным документом, страницей и версией. Плановые материалы, фактические работы и документы качества учитываются раздельно.">
    {project && canWrite && <button className="btn" disabled={busy} onClick={() => void perform(async () => { const r = await api<{ remaining: number; processed: number }>("/brain/facts/refresh", { method: "POST", json: { limit: 30 } }); setNotice(`Разобрано новых или изменённых документов: ${r.processed}.${r.remaining ? ` Осталось: ${r.remaining}.` : ""}`); })}><Icon name="brain" size={16} />{busy ? "Сверяю источники…" : "Разобрать документы"}</button>}
  </PageHead>
  {!project ? <div className="panel"><h2>Выберите проект</h2><p className="muted">Укажите объект в верхней панели. Его факты и противоречия будут показаны отдельно от остальных проектов.</p></div> : <>
    {error && <div role="alert" className="alert error">{error}</div>}{notice && <div className="note">{notice}</div>}
    <div className="fact-overview"><div><span className="fact-overview-value">{registry?.coverage.indexed_documents ?? "—"}<small> / {registry?.coverage.documents ?? "—"}</small></span><span>исходных документов разобрано</span></div><div><span className="fact-overview-value">{open.length}</span><span>противоречий требуют сверки</span></div><p>Реестр хранит основания.<br />Исходные документы остаются без изменений.</p></div>
    <div className="panel fact-investigation"><h2>Сверить сведения в источниках</h2><p className="muted">Например: когда завершили прокладку кабеля на участке 123, какие материалы применены или какой объём учтён.</p>
      <form onSubmit={(e) => { e.preventDefault(); void perform(async () => { setResult(await api<Investigation>("/brain/facts/investigate", { method: "POST", json: { question, budget: 20 } })); }); }}>
        <label className="sr-only" htmlFor="fact-question">Вопрос о фактах проекта</label><div className="row"><input id="fact-question" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Когда завершили прокладку кабеля на участке 123?" /><button type="submit" className="btn primary" disabled={busy || question.trim().length < 3}>Проверить источники <Icon name="arrow" size={15} /></button></div>
      </form>
      {busy && <p className="muted small" role="status">Проверяю реестр и исходные документы. Непрочитанные файлы не считаются отсутствующими данными.</p>}
      {result && <div className="investigation-result"><span className={`badge ${result.state === "ready" ? "active" : "draft"}`}>{STATES[result.state] ?? result.state}</span><p className="investigation-answer">{result.text}</p>
        <details><summary>Порядок и охват проверки</summary><ol>{result.plan.steps.map((s) => <li key={s}>{s}</li>)}</ol><p className="small muted">Проверено источников: {result.coverage.read} из {result.coverage.candidates}. Из реестра: {result.coverage.cached}. Осталось: {result.coverage.remaining}.</p></details>
        {result.facts.slice(0, 12).map((f) => <Evidence key={f.id} fact={f} />)}
      </div>}
    </div>
    <div className="fact-tabs" role="tablist" aria-label="Реестр проекта">{[["facts", "Факты"], ["conflicts", `Противоречия${open.length ? ` · ${open.length}` : ""}`], ["history", "Проверки"]].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "selected" : ""} onClick={() => setTab(id)}>{label}</button>)}</div>
    {tab === "facts" && <><form className="fact-filters" onSubmit={(e) => { e.preventDefault(); setApplied({ ...filters }); }}><div className="field"><label htmlFor="fact-field">Сведения</label><select id="fact-field" value={filters.predicate} onChange={(e) => setFilters({ ...filters, predicate: e.target.value })}><option value="">Все сведения</option>{Object.entries(registry?.labels ?? {}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      <div className="field"><label htmlFor="fact-category">Происхождение</label><select id="fact-category" value={filters.category} onChange={(e) => setFilters({ ...filters, category: e.target.value })}><option value="">Все основания</option>{Object.entries(CATEGORIES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      <div className="field"><label htmlFor="fact-location">Место</label><input id="fact-location" value={filters.location} onChange={(e) => setFilters({ ...filters, location: e.target.value })} placeholder="Участок 123" /></div><button className="btn" type="submit">Показать</button></form>
      {registry?.truncated && <div className="note warning">Показана часть реестра. Уточните вид сведений или место.</div>}
      {registry?.coverage.unread.map((name) => <div className="note warning" key={name}>Не прочитан источник: {name}</div>)}
      {registry?.coverage.stale.length ? <div className="note warning">Есть изменённые источники: {registry.coverage.stale.length}. Разберите документы повторно.</div> : null}
      {registry?.items.length === 0 ? <div className="panel fact-empty"><Icon name="facts" size={28} /><h2>Подтверждённых записей пока нет</h2><p className="muted">Это не означает, что сведений нет в архиве. Запустите разбор документов или задайте вопрос выше.</p></div> : <div className="fact-grid">{registry?.items.map((f) => <article className="panel fact-card" key={f.id}><div className="fact-card-header"><span>{f.label}</span><span className={`badge fact-category-${f.category}`}>{CATEGORIES[f.category] ?? f.category}</span></div><h3>{value(f)}</h3><p className="fact-scope">{f.location || f.location_key || "Место отдельно не указано"}{f.work_key ? ` · ${f.work_key.split("|").join(" / ")}` : ""}</p>{f.state !== "active" && <span className="text-warn small">Извлечение неполное — требуется проверка</span>}<Evidence fact={f} /></article>)}</div>}
    </>}
    {tab === "conflicts" && <div>{conflicts.length === 0 && <div className="panel fact-empty"><Icon name="check" size={28} /><h2>Противоречий в разобранных данных не найдено</h2><p className="muted">Охват исходных документов указан выше.</p></div>}{conflicts.map((c) => <section className="panel fact-conflict" key={c.id}><div className="pto-section-head"><h2>{c.message}</h2><span className={`badge ${c.state === "resolved" ? "active" : "draft"}`}>{STATES[c.state] ?? c.state}</span></div>
      <div className="conflict-values">{c.facts.map((f) => <div className="note" key={f.id}><strong>{value(f)}</strong><p className="small muted">{CATEGORIES[f.category]} · {f.source_title}</p><Evidence fact={f} /></div>)}</div>
      {c.state === "resolved" && <p className="note">Основание выбора: {c.reason}</p>}
      {canWrite && c.state === "open" && c.kind === "different_values" && <form className="fact-resolution" onSubmit={(e) => { e.preventDefault(); void perform(async () => { await api(`/brain/facts/conflicts/${c.id}/decision`, { method: "POST", json: { fact_id: choices[c.id], revision: c.revision, reason: reasons[c.id] } }); setNotice("Выбор сохранён с автором и основанием. Исходные значения не изменены."); }); }}>
        <div className="field"><label htmlFor={`choose-${c.id}`}>Подтверждённое значение</label><select id={`choose-${c.id}`} value={choices[c.id] ?? ""} onChange={(e) => setChoices({ ...choices, [c.id]: e.target.value })}><option value="">Выберите из источников</option>{c.facts.map((f) => <option key={f.id} value={f.id}>{value(f)} — {f.source_title}</option>)}</select></div>
        <div className="field"><label htmlFor={`reason-${c.id}`}>На каком основании сделан выбор</label><input id={`reason-${c.id}`} value={reasons[c.id] ?? ""} onChange={(e) => setReasons({ ...reasons, [c.id]: e.target.value })} placeholder="Например: сверено с подписанной записью журнала" /></div><button type="submit" className="btn primary" disabled={busy || !choices[c.id] || (reasons[c.id] ?? "").trim().length < 3}>Сохранить выбор</button>
      </form>}
      {c.kind === "material_mismatch" && <p className="small muted">Замена материала требует документа-основания или исправления фактических сведений. Выбором строки это расхождение не снимается.</p>}
    </section>)}</div>}
    {tab === "history" && <div className="panel"><h2>История проверок проекта</h2>{history.length === 0 && <p className="muted">Проверок пока нет.</p>}{history.map((h) => <div className="fact-history-item" key={h.id}><div><strong>{h.question}</strong><p className="small muted">{new Date(h.created_at).toLocaleString("ru-RU")}</p></div><span className="badge">{STATES[h.state] ?? h.state}</span></div>)}</div>}
  </>}
  </div>;
}

import { useEffect, useRef, useState } from "react";
import { api, download, openFile, upload, activeProject, type User } from "../../api";
import { PageHead, useObjects, VoiceButton } from "../../components";
import { navigate } from "../navigation";

interface Ref { id: string; title?: string; name?: string; kind?: string; number?: string | null }
interface Gap { field: string; message: string; candidates: Ref[] }
interface Evidence { field: string; value: string; document_id: string | null; page: number | null; quote: string; method: string }
interface Requirement { id: string; label: string; value: string; quote: string; state: string; document_ids: string[]; source: { title: string; document_id?: string; library_version_id?: string; page?: number } }
interface Readiness { state: string; blockers: { field: string; message: string }[]; requirements: Requirement[]; source_documents: number; checked_at: string; note: string }
interface DesignRow { reading_id: string; document_id: string; position: string | null; name: string; mark: string | null; quantity: string | null; unit: string | null; element: string | null; location: string | null; code: string | null; sheet: string | null; page: number | null; quote: string }
interface Alignment { state: string; target: { location: string | null; element: string | null }; checks: { material: string; state: string; candidates: DesignRow[] }[]; issues: { field: string; message: string }[]; limits: string }
interface Run {
  id: string; text: string; status: string; revision: number; object_id: string | null; error?: string;
  steps: { name: string; details: string; status: string }[]; gaps?: Gap[]; documents?: Ref[]; attachments?: Ref[]; evidence?: Evidence[];
  plan?: { act_kind: string; works: string; location: string; project_docs: string; planned_materials: string[]; materials: string[];
    work_start: string | null; work_end: string | null; next_works: string | null; requirements: string[]; project_alignment?: Alignment };
}
const STATUS: Record<string, string> = { queued: "Задание сохранено", running: "Синапс собирает комплект", needs_data: "Нужны конкретные уточнения", ready_for_review: "Черновик готов к проверке", error: "Обработка не завершена" };
const FIELDS: Record<string, string> = { work_start: "Начало работ", work_end: "Окончание работ", actual_material: "Фактический материал", project_ref: "Рабочая документация", quality: "Документ качества", protocol_result: "Заключение испытаний", journal_entry: "Запись журнала", next_work: "Последующие работы", batch: "Партия", element: "Линия / элемент" };

export default function ExecutivePipeline({ user }: { user: User }) {
  const objects = useObjects();
  const [text, setText] = useState("");
  const [objectId, setObjectId] = useState(activeProject());
  const [audio, setAudio] = useState<File | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [checking, setChecking] = useState(false);
  const readinessRequest = useRef(0);
  const [correction, setCorrection] = useState({ location: "", element: "", materials: "", batch: "", work_start: "", work_end: "", act_kind: "" });
  const [choices, setChoices] = useState<Record<string, string>>({});
  const submitted = useRef<{ signature: string; id: string } | null>(null);
  const audioInput = useRef<HTMLInputElement>(null);
  const storageKey = `bis:executive-run:${user.organization_id}:${activeProject()}`;
  const loadList = () => api<Run[]>("/executive/runs").then(setRuns);
  useEffect(() => {
    void loadList().catch((e) => setError(e.message));
    try { setRunId(localStorage.getItem(storageKey)); } catch { /* optional persistence */ }
  }, [storageKey]);
  useEffect(() => { if (activeProject()) setObjectId(activeProject()); else if (objects.length === 1) setObjectId(objects[0].id); }, [objects]);
  useEffect(() => {
    if (!runId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await api<Run>(`/executive/runs/${runId}`);
        if (!active) return;
        setRun(result);
        if (["queued", "running"].includes(result.status)) timer = setTimeout(poll, 1500);
        else void loadList().catch(() => {});
      } catch (e) { if (active) setError((e as Error).message); }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [runId, run?.revision]);

  function select(id: string | null, result: Run | null = null) {
    readinessRequest.current += 1;
    setReadiness(null); setChecking(false);
    setRunId(id); setRun(result); setError(""); setChoices({});
    setCorrection({ location: "", element: "", materials: "", batch: "", work_start: "", work_end: "", act_kind: "" });
    if (!id) {
      setText(""); setAudio(null); submitted.current = null;
      if (audioInput.current) audioInput.current.value = "";
    }
    try { if (id) localStorage.setItem(storageKey, id); else localStorage.removeItem(storageKey); } catch { /* optional persistence */ }
  }
  async function submit() {
    setBusy(true); setError("");
    const signature = JSON.stringify([text, objectId, audio?.name, audio?.size, audio?.lastModified]);
    if (submitted.current?.signature !== signature) submitted.current = { signature, id: crypto.randomUUID() };
    try {
      let result: Run;
      if (audio) {
        const form = new FormData(); form.append("file", audio); form.append("request_id", submitted.current!.id);
        if (objectId) form.append("object_id", objectId);
        result = await upload<Run>("/executive/runs/voice", form);
      } else result = await api<Run>("/executive/runs", { method: "POST", json: { text, object_id: objectId || null, request_id: submitted.current!.id } });
      select(result.id, result); void loadList();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function resume() {
    if (!run) return;
    setBusy(true); setError("");
    const body: Record<string, unknown> = { revision: run.revision, choices: Object.fromEntries(Object.entries(choices).map(([key, value]) => [key, value || null])) };
    for (const [key, value] of Object.entries(correction)) if (value.trim()) body[key] = key === "materials" ? value.split("\n").map((x) => x.trim()).filter(Boolean) : value;
    if (!run.object_id && objectId) body.object_id = objectId;
    try {
      const result = await api<Run>(`/executive/runs/${run.id}/resume`, { method: "POST", json: body });
      setRun(result); setChoices({});
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const processing = !!run && ["queued", "running"].includes(run.status);
  const canWrite = user.role !== "viewer";
  const plan = run?.plan;
  const outdated = run?.status === "ready_for_review" && readiness?.state === "blocked";
  const showCorrection = run && !processing && run.status !== "ready_for_review";
  async function checkReadiness() {
    if (!runId || !run || run.id !== runId || processing) return;
    const request = ++readinessRequest.current;
    setChecking(true);
    try {
      const result = await api<Readiness>(`/executive/runs/${runId}/readiness`);
      if (request === readinessRequest.current) setReadiness(result);
    } catch (e) {
      if (request === readinessRequest.current) setError((e as Error).message);
    } finally {
      if (request === readinessRequest.current) setChecking(false);
    }
  }
  useEffect(() => {
    readinessRequest.current += 1;
    setReadiness(null);
    setChecking(false);
    if (runId && run?.id === runId && !processing) void checkReadiness();
    return () => { readinessRequest.current += 1; };
  }, [runId, run?.id, run?.status, run?.revision]);

  return <div className="pipeline-workspace">
    <PageHead title="Сообщить о выполненной работе" hint="Сообщение или голос → участок → РД и журнал → документы качества и испытаний → черновик акта с приложениями. Учёт и списание материалов не выполняются." />
    {error && <div className="alert error" role="alert">{error}</div>}
    <div className="panel">
      {!activeProject() && <div className="field"><label htmlFor="pipeline-object">Объект строительства</label><select id="pipeline-object" value={objectId} onChange={(e) => setObjectId(e.target.value)}>
        <option value="">Определить автоматически, если объект один</option>{objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
      </select></div>}
      <div className="field"><label htmlFor="pipeline-report">Что выполнено и где</label><textarea id="pipeline-report" value={text} disabled={!!audio} onChange={(e) => setText(e.target.value)}
        placeholder="Например: на участке 123 проложен кабель. Синапс найдёт РД, даты в журнале, документы качества и необходимые приложения." /></div>
      <div className="row" style={{ alignItems: "center" }}><VoiceButton onText={(value) => setText((old) => `${old} ${value}`.trim())} />
        <details className="audio-upload"><summary>{audio ? audio.name : "Загрузить аудиозапись"}</summary><div className="field" style={{ margin: 0 }}><label htmlFor="pipeline-audio">Или загрузите голосовое сообщение</label><input ref={audioInput} id="pipeline-audio" type="file" accept="audio/*,.ogg,.opus,.webm,.mp3,.wav,.m4a" onChange={(e) => setAudio(e.target.files?.[0] ?? null)} /></div></details>
        <button className="btn primary" disabled={busy || processing || !canWrite || (!audio && text.trim().length < 3)} onClick={() => void submit()}>{busy ? "Сохраняю…" : "Собрать комплект ИД"}</button>
      </div>
      <p className="muted small">При загрузке записи используется только её расшифровка. Даты и марки, которых нет в сообщении, система ищет в источниках.</p>
    </div>

    {run && <div className="panel" aria-live="polite">
      <div className="pto-section-head"><h2>{outdated ? "Комплект требует повторной проверки" : STATUS[run.status] ?? run.status}</h2><button className="btn small" onClick={() => select(null)}>Новое сообщение</button></div>
      <p>{run.text}</p>{run.error && <div className="alert error">{run.error}</div>}
      <details className="pipeline-progress" open={processing || undefined}><summary>{processing ? "Ход подготовки" : "Как собран комплект"}</summary><ol className="pipeline-steps">{run.steps.map((step, i) => <li key={i}>{step.details}</li>)}</ol></details>
      {processing && <p className="muted">Обработка идёт в фоне. Можно закрыть страницу — задание сохранено.</p>}
      {!processing && canWrite && run.status === "ready_for_review" && <button className="btn" disabled={busy} onClick={() => void resume()}>Обновить основания и повторить проверку</button>}
      {readiness && !processing && <section className="package-readiness">
        <div className="pto-section-head"><h3>Контроль комплекта</h3><button className="btn small" disabled={checking} onClick={() => void checkReadiness()}>{checking ? "Проверяю…" : "Проверить актуальность"}</button></div>
        <p className={`note ${readiness.state === "ready_for_review" ? "" : "warning"}`}>{readiness.state === "ready_for_review" ? "Комплект готов к проверке инженером" : "Выпуск требует устранения замечаний"}</p>
        {readiness.blockers.length > 0 && <ul>{readiness.blockers.map((b, i) => <li key={`${b.field}:${i}`}>{b.message}</li>)}</ul>}
        <div className="requirement-list">{readiness.requirements.map((r) => <details className="requirement-row" key={r.id}>
          <summary><span><small className="muted">{r.label}</small><strong>{r.value}</strong></span><span className={`badge ${r.state === "satisfied" ? "active" : "draft"}`}>{{ satisfied: "Основание найдено", missing: "Не закрыто", conditional: "Проверить условие", not_required: "Не требуется по источнику", conflict: "Противоречие", invalid: "Не прошло проверку" }[r.state] ?? r.state}</span></summary>
          <blockquote>{r.quote}</blockquote><p className="small muted">Основание: {r.source.title}{r.source.page ? `, стр. ${r.source.page}` : ""}</p>
          {(r.source.document_id || r.source.library_version_id) && <button className="link small" onClick={() => void openFile(r.source.library_version_id ? `/libraries/versions/${r.source.library_version_id}/file` : `/documents/${r.source.document_id}/file`, r.source.title, r.source.page)}>Открыть основание</button>}
          {r.document_ids.map((id) => <button className="btn small" key={id} onClick={() => void openFile(`/documents/${id}/file`, "Приложение")}>{run.attachments?.find((a) => a.id === id)?.title ?? "Подтверждающий документ"}</button>)}
        </details>)}</div>
        <p className="small muted">{readiness.source_documents} исходных документов под контролем версий. Проверено: {new Date(readiness.checked_at).toLocaleString("ru-RU")}.</p><p className="small muted">{readiness.note}</p>
      </section>}
      {!!run.gaps?.length && <div className="note warning"><h3>Что требует уточнения</h3>{run.gaps.map((g) => <div className="field" key={g.field}>
        <p>{g.message}</p>{g.candidates.length > 0 && <select aria-label={g.message} value={g.field === "object_id" ? objectId : choices[g.field] ?? ""} disabled={processing || !canWrite}
          onChange={(e) => g.field === "object_id" ? setObjectId(e.target.value) : setChoices((old) => ({ ...old, [g.field]: e.target.value }))}>
          <option value="">Выберите из найденных вариантов</option>{g.candidates.map((c) => <option key={c.id} value={c.id}>{c.title ?? c.name ?? c.number}</option>)}
        </select>}
      </div>)}</div>}
      {plan && <>
        {plan.project_alignment && <section className="package-readiness"><div className="pto-section-head"><h3>Сверка с проектом</h3><span className={`badge ${plan.project_alignment.state === "aligned" ? "active" : "draft"}`}>{{ aligned: "Позиции сопоставлены", conflict: "Расхождение с РД", ambiguous: "Нужно уточнить позицию", partial: "Чтение неполное", insufficient: "Недостаточно оснований" }[plan.project_alignment.state] ?? plan.project_alignment.state}</span></div>
          <p className="small muted">{[plan.project_alignment.target.location, plan.project_alignment.target.element].filter(Boolean).join(" · ")}</p>
          {plan.project_alignment.checks.map((check, i) => <details className="requirement-row" key={i}><summary><strong>{check.material}</strong><span className="small muted">{{ matched: "Найдена позиция", ambiguous: "Несколько позиций", unconfirmed: "Не подтверждено", mismatch: "Не соответствует" }[check.state] ?? check.state}</span></summary>
            {check.candidates.map((row, j) => <div className="note" key={j}><strong>{[row.code, row.sheet ? `лист ${row.sheet}` : "", row.position ? `позиция ${row.position}` : "", row.element].filter(Boolean).join(" · ") || "Запись проекта"}</strong><p>{row.name} {row.mark ?? ""}{row.quantity ? ` · ${row.quantity} ${row.unit ?? ""}` : ""}</p><blockquote>{row.quote}</blockquote><button className="link small" onClick={() => void openFile(`/projects/readings/${row.reading_id}/file`, "Основание РД", row.page ?? undefined)}>Открыть исходную версию{row.page ? `, стр. ${row.page}` : ""}</button></div>)}
          </details>)}
          <p className="small muted">{plan.project_alignment.limits}</p><button className="btn small" onClick={() => navigate("id", "project-reading")}>Проверить чтение проекта</button>
        </section>}
        <div className="stats"><div className="stat"><div className="stat-value">{plan.act_kind === "aook" ? "АООК" : "АОСР"}</div><div className="stat-label">Черновик акта</div></div>
          <div className="stat"><div>{plan.location}</div><div className="stat-label">Место выполнения</div></div>
          <div className="stat"><div>{plan.work_start ?? "Дата не подтверждена"} → {plan.work_end ?? "Дата не подтверждена"}</div><div className="stat-label">Период по фактическим источникам</div></div></div>
        <p><strong>Рабочая документация:</strong> {plan.project_docs || "Основание ещё не найдено"}</p>
        <div className="grid2"><div><h3>Предусмотрено проектом</h3><ul>{plan.planned_materials.map((m, i) => <li key={i}>{m}</li>)}</ul></div>
          <div><h3>Фактические материалы и документы качества</h3>{plan.materials.length ? <ul>{plan.materials.map((m, i) => <li key={i}>{m}</li>)}</ul> : <p className="muted">Фактическое применение ещё не подтверждено.</p>}</div></div>
        <p><strong>Дальше по РД:</strong> {plan.next_works ?? "Последующая операция не подтверждена проектом"}</p>
        <p className="small muted">Следующая операция по проекту не означает разрешения на её выполнение: необходимые освидетельствования и испытания должны быть завершены.</p>
      </>}
      {showCorrection && canWrite && <div className="panel"><h3>Уточнить недостающие факты</h3><p className="muted small">Заполняйте только подтверждённые сведения. Они сохранятся как ваше уточнение с указанием автора.</p>
        <div className="row">{([["location", "Участок / помещение / узел"], ["element", "Линия / элемент по проекту"], ["batch", "Фактическая партия"], ["work_start", "Фактическое начало"], ["work_end", "Фактическое окончание"]] as const).map(([key, label]) => <div className="field" key={key}><label htmlFor={`correct-${key}`}>{label}</label><input id={`correct-${key}`} type={key.startsWith("work_") ? "date" : "text"} value={correction[key]} onChange={(e) => setCorrection({ ...correction, [key]: e.target.value })} /></div>)}</div>
        <div className="field"><label htmlFor="correct-materials">Фактически применённые марки — по строке</label><textarea id="correct-materials" value={correction.materials} onChange={(e) => setCorrection({ ...correction, materials: e.target.value })} /></div>
        {run.gaps?.some((g) => g.field === "act_kind") && <div className="field"><label htmlFor="correct-kind">Подтверждённый вид акта</label><select id="correct-kind" value={correction.act_kind} onChange={(e) => setCorrection({ ...correction, act_kind: e.target.value })}><option value="">Определить по РД</option><option value="aosr">АОСР</option><option value="aook">АООК</option></select></div>}
        <div className="row"><button className="btn primary" disabled={busy} onClick={() => void resume()}>Учесть уточнения и повторить проверку</button><button className="btn" onClick={() => navigate("id", "registry:appointments")}>Проверить участников и назначения</button></div>
      </div>}
      {!!run.documents?.length && <div className="row"><button className="btn primary" disabled={processing} onClick={() => void download(`/executive/runs/${run.id}/bundle`, "Комплект_ИД.zip").catch((e) => setError(e.message))}>Скачать черновик и приложения ZIP</button>
        {run.documents.map((doc) => <button className="btn" key={doc.id} onClick={() => void openFile(`/documents/${doc.id}/file`, doc.title ?? "Акт")}>{doc.title}</button>)}
      </div>}
      {!!run.attachments?.length && <><h3>Приложения к комплекту</h3><div className="doc-cards">{run.attachments.map((doc) => <button className="btn small" key={doc.id} onClick={() => void openFile(`/documents/${doc.id}/file`, doc.title ?? "Документ")}>{doc.title}</button>)}</div></>}
      {!!run.evidence?.length && <details className="pipeline-evidence"><summary>На чём основано заполнение — {run.evidence.length} фактов</summary>{run.evidence.map((e, i) => <div className="note" key={i}>
        <strong>{FIELDS[e.field] ?? "Основание"}: {e.field === "protocol_result" ? ({ passed: "Соответствует", failed: "Не соответствует", unknown: "Не подтверждено" }[e.value] ?? e.value) : e.value}</strong><p>{e.quote}</p>{e.document_id && <button className="link small" onClick={() => void openFile(`/documents/${e.document_id}/file`, "Источник", e.page ?? undefined)}>Открыть источник{e.page ? `, стр. ${e.page}` : ""}</button>}
      </div>)}</details>}
      <p className="muted small">Комплект создаётся как черновик. При наличии ошибок утверждение заблокировано.</p>
    </div>}
    {runs.length > 0 && <div className="panel"><h2>Сохранённые задания</h2><div className="pipeline-runs">{runs.map((item) => <button key={item.id} className={`pto-action ${runId === item.id ? "selected" : ""}`} onClick={() => select(item.id)}><strong>{item.text}</strong><small>{STATUS[item.status] ?? item.status}</small></button>)}</div></div>}
  </div>;
}

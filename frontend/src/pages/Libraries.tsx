import { useEffect, useState } from "react";
import { activeProject, api, fetchBlob, openFile, type User } from "../api";
import { PageHead, useObjects } from "../components";

type Level = "knowledge" | "organization" | "project";
interface Connection { id: string; level: Level; object_id: string | null; name: string; path: string; enabled: boolean; status: string; error: string | null; synced_at: string | null }
interface Entry { field: string; target: string; anchor: string; mode: string }
interface Mapping { format: string; entries: Entry[]; targets?: Record<string, string> }
interface Version { id: string; revision: number; form_kind: string | null; mapping: Mapping; approved_at: string | null; source_id: string | null }
interface FileRow { id: string; path: string; state: string; error: string | null; active_version_id: string; versions: Version[] }
interface LibraryState { strict: boolean; connections: Connection[]; allowed_roots: string[]; form_fields: Record<string, string>; platform_admin: boolean }
const LEVELS = { knowledge: "База знаний", organization: "База организации", project: "База проекта" };
const STATUS: Record<string, string> = { pending: "Ожидает чтения", ready: "Прочитано", partial: "Есть ошибки чтения", error: "Ошибка", removed: "Файл удалён из папки" };
const KINDS = { aosr: "АОСР", aook: "АООК", letter: "Письмо", work_journal: "Журнал работ", id_registry: "Реестр ИД" };

export default function Libraries({ user }: { user: User }) {
  const objects = useObjects();
  const [state, setState] = useState<LibraryState>();
  const [selected, setSelected] = useState("");
  const [files, setFiles] = useState<FileRow[]>([]);
  const [unassigned, setUnassigned] = useState<{ id: string; title: string }[]>([]);
  const [form, setForm] = useState({ name: "", path: "", level: "organization" as Level, object_id: activeProject() });
  const [editing, setEditing] = useState<Version>();
  const [kind, setKind] = useState("aosr");
  const [mapping, setMapping] = useState<Mapping>({ format: "docx", entries: [] });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const canWrite = user.role === "admin";
  const load = () => api<LibraryState>("/libraries").then(setState);
  const loadFiles = (id: string) => api<FileRow[]>(`/libraries/${id}/files`).then(setFiles);
  useEffect(() => {
    void load().catch((e) => setError(e.message));
    if (canWrite) void api<typeof unassigned>("/libraries/unassigned/list").then(setUnassigned).catch((e) => setError(e.message));
    const timer = setInterval(() => { void load().catch(() => {}); if (selected) void loadFiles(selected).catch(() => {}); }, 5000);
    return () => clearInterval(timer);
  }, [selected]);
  async function action(fn: () => Promise<unknown>, success: string) {
    setBusy(true); setError(""); setMessage("");
    try { await fn(); await load(); if (selected) await loadFiles(selected); setMessage(success); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function edit(v: Version) {
    await action(async () => {
      const m = v.approved_at ? v.mapping : await api<Mapping>(`/libraries/versions/${v.id}/prepare`, { method: "POST" });
      setEditing(v); setKind(v.form_kind ?? "aosr"); setMapping(m);
    }, "Карта заполнения открыта");
  }
  function change(index: number, patch: Partial<Entry>) { setMapping({ ...mapping, entries: mapping.entries.map((e, i) => i === index ? { ...e, ...patch } : e) }); }
  async function preview() {
    const blob = await fetchBlob(`/libraries/versions/${editing!.id}/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, mapping }) });
    const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url;
    link.download = `Проверка_формы.${mapping.format.startsWith("pdf") ? "pdf" : mapping.format}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  return <div className="libraries"><PageHead title="Библиотеки Синапса" />
    <p className="muted">Нормативы и общие формы → документы организации → отдельная база выбранного проекта. Оригиналы в папках остаются без изменений.</p>
    {error && <div className="alert error">{error}</div>}{message && <div className="note">{message}</div>}
    <div className="panel"><h2>Источники ответов и форм</h2>
      <p>{state?.strict ? "Строгий режим: только подключённые библиотеки и данные активного проекта. Встроенные формы не подставляются." : "Строгий режим пока выключен. После подключения библиотек включите его для этой организации."}</p>
      {canWrite && <button className="btn" disabled={busy || !state} onClick={() => void action(() => api("/libraries/policy", { method: "PUT", json: { strict: !state?.strict } }), "Режим источников обновлён")}>{state?.strict ? "Выключить строгий режим" : "Включить строгий режим"}</button>}
    </div>
    {canWrite && <div className="panel"><h2>Подключить папку</h2>
      <p className="small muted">Укажите путь на компьютере, где работает сервер BIS. Разрешённые каталоги: {state?.allowed_roots.join(", ") || "ещё не настроены; задайте BIS_LIBRARY_ROOTS при запуске"}.</p>
      <div className="grid2"><div className="field"><label htmlFor="library-name">Название подключения</label><input id="library-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
        <div className="field"><label htmlFor="library-path">Путь к папке</label><input id="library-path" value={form.path} onChange={(e) => setForm({ ...form, path: e.target.value })} placeholder="D:\BIS\База организации" /></div>
        <div className="field"><label htmlFor="library-level">Уровень базы</label><select id="library-level" value={form.level} onChange={(e) => setForm({ ...form, level: e.target.value as Level })}>
          {Object.entries(LEVELS).map(([key, label]) => <option key={key} value={key} disabled={key === "knowledge" && !state?.platform_admin}>{label}{key === "knowledge" && !state?.platform_admin ? " — администратор платформы" : ""}</option>)}</select></div>
        {form.level === "project" && <div className="field"><label htmlFor="library-project">Проект</label><select id="library-project" value={form.object_id} onChange={(e) => setForm({ ...form, object_id: e.target.value })}><option value="">Выберите проект</option>{objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}</select></div>}
      </div>
      <button className="btn primary" disabled={busy || !form.name || !form.path || (form.level === "project" && !form.object_id)} onClick={() => void action(async () => {
        await api("/libraries", { method: "POST", json: { ...form, object_id: form.level === "project" ? form.object_id : null } });
        setForm({ ...form, name: "", path: "" });
      }, "Папка подключена. Чтение выполняется в фоне.")}>Подключить и прочитать</button>
    </div>}
    <div className="panel"><h2>Подключённые папки</h2>{state?.connections.length === 0 && <p className="muted">Папки пока не подключены.</p>}
      {state?.connections.map((c) => <div className="note" key={c.id}><strong>{LEVELS[c.level]}: {c.name}</strong><p className="small">{c.path}</p><p>{c.enabled ? STATUS[c.status] ?? c.status : "Отключено"}{c.synced_at ? ` · ${new Date(c.synced_at).toLocaleString("ru-RU")}` : ""}</p>{c.error && <p className="alert error">{c.error}</p>}
        <div className="row"><button className="btn small" onClick={() => void action(async () => { setSelected(c.id); await loadFiles(c.id); setEditing(undefined); }, "Файлы загружены")}>Файлы и версии</button>
          {canWrite && (c.level !== "knowledge" || state.platform_admin) && <><button className="btn small" disabled={busy || !c.enabled} onClick={() => void action(() => api(`/libraries/${c.id}/sync`, { method: "POST" }), "Проверка изменений запущена")}>Синхронизировать сейчас</button>
            <button className="btn small" disabled={busy} onClick={() => void action(() => api(`/libraries/${c.id}?enabled=${!c.enabled}`, { method: "PATCH" }), "Подключение обновлено")}>{c.enabled ? "Отключить" : "Включить"}</button></>}
        </div></div>)}
    </div>
    {selected && <div className="panel"><h2>Файлы выбранной папки</h2>{!files.length && <p>Файлов пока нет.</p>}{files.map((f) => <div className="note" key={f.id}><strong>{f.path}</strong><p>{STATUS[f.state] ?? f.state}</p>{f.error && <p className="alert error">{f.error}</p>}
      {f.versions.map((v) => <div className="row" key={v.id}><span>Версия {v.revision}{f.active_version_id === v.id ? " · текущая" : " · история"}{v.approved_at ? " · форма подтверждена" : ""}</span>
        <button className="btn small" onClick={() => void openFile(`/libraries/versions/${v.id}/file`, f.path)}>Оригинал</button>
        {canWrite && /\.(docx|xlsx|pdf)$/i.test(f.path) && f.state === "ready" && f.active_version_id === v.id && <button className="btn small" disabled={busy} onClick={() => void edit(v)}>Настроить заполнение формы</button>}
        {canWrite && v.source_id && !v.form_kind && <button className="btn small" disabled={busy} onClick={() => void action(() => api(`/knowledge/sources/${v.source_id}`, { method: "PATCH", json: { status: "active" } }), "Статус редакции подтверждён")}>Подтвердить действующую редакцию</button>}
      </div>)}
    </div>)}</div>}
    {editing && <div className="panel"><h2>Проверка карты заполнения</h2><p>Сопоставьте поля, скачайте пробный результат и подтвердите эту версию. PDF без полей: адрес области — страница:x:y:ширина:высота, координаты в пунктах от верхнего левого края.</p>
      <div className="row"><div className="field"><label htmlFor="form-kind">Вид документа</label><select id="form-kind" value={kind} disabled={!!editing.approved_at} onChange={(e) => setKind(e.target.value)}>{Object.entries(KINDS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      <div className="field"><label htmlFor="mapping-format">Способ заполнения</label><select id="mapping-format" value={mapping.format} disabled={!!editing.approved_at} onChange={(e) => setMapping({ ...mapping, format: e.target.value })}><option value="docx">Word</option><option value="xlsx">Excel</option><option value="pdf">Поля PDF</option><option value="pdf_coordinates">Области PDF</option></select></div></div>
      {mapping.entries.map((entry, index) => <div className="panel" key={index}><div className="row"><select aria-label={`Поле ${index + 1}`} value={entry.field} disabled={!!editing.approved_at} onChange={(e) => change(index, { field: e.target.value })}>{Object.entries(state?.form_fields ?? {}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
        {mapping.targets && Object.keys(mapping.targets).length > 0 ? <select aria-label={`Адрес ${index + 1}`} value={entry.target} disabled={!!editing.approved_at} onChange={(e) => change(index, { target: e.target.value, anchor: mapping.targets?.[e.target.value] ?? "" })}><option value="">Выберите место в бланке</option>{Object.entries(mapping.targets).map(([target, label]) => <option key={target} value={target}>{target}: {label.slice(0, 100) || "Пустая строка"}</option>)}</select> : <input aria-label={`Адрес ${index + 1}`} value={entry.target} disabled={!!editing.approved_at} onChange={(e) => change(index, { target: e.target.value })} />}
        <select aria-label={`Заполнение ${index + 1}`} value={entry.mode} disabled={!!editing.approved_at} onChange={(e) => change(index, { mode: e.target.value })}><option value="append">Сохранить подпись поля</option><option value="replace">Заменить содержимое</option></select>
        {!editing.approved_at && <button className="btn small" onClick={() => setMapping({ ...mapping, entries: mapping.entries.filter((_, i) => i !== index) })}>Убрать поле</button>}</div>
        <div className="field"><label>Исходная подпись / текст ячейки</label><input value={entry.anchor} disabled={!!editing.approved_at} onChange={(e) => change(index, { anchor: e.target.value })} /></div>
      </div>)}
      <div className="row">{!editing.approved_at && <button className="btn" onClick={() => setMapping({ ...mapping, entries: [...mapping.entries, { field: "works", target: "", anchor: "", mode: "append" }] })}>Добавить поле</button>}
        <button className="btn" disabled={busy} onClick={() => void action(preview, "Пробная форма скачана")}>Скачать пробное заполнение</button>
        {!editing.approved_at ? <button className="btn primary" disabled={busy} onClick={() => void action(async () => { await api(`/libraries/versions/${editing.id}/approve`, { method: "POST", json: { kind, mapping } }); setEditing(undefined); }, "Карта подтверждена для этой версии формы")}>Подтвердить карту</button> :
          <button className="btn" disabled={busy} onClick={() => void action(async () => { await api(`/libraries/versions/${editing.id}/revoke`, { method: "POST" }); setEditing(undefined); }, "Подтверждение отозвано")}>Отозвать подтверждение</button>}
      </div>
    </div>}
    {canWrite && unassigned.length > 0 && <div className="panel"><h2>Нужно распределить</h2><p>Эти документы не используются во всех проектах автоматически.</p>{unassigned.map((d) => <div className="note" key={d.id}><strong>{d.title}</strong><div className="row"><button className="btn small" disabled={busy || !activeProject()} onClick={() => void action(async () => { await api(`/libraries/unassigned/${d.id}`, { method: "POST", json: { level: "project", object_id: activeProject() } }); setUnassigned(unassigned.filter((x) => x.id !== d.id)); }, "Документ отнесён к активному проекту")}>Отнести к активному проекту</button>
      <button className="btn small" disabled={busy} onClick={() => void action(async () => { await api(`/libraries/unassigned/${d.id}`, { method: "POST", json: { level: "organization" } }); setUnassigned(unassigned.filter((x) => x.id !== d.id)); }, "Документ отнесён к организации")}>Документ организации</button></div></div>)}</div>}
  </div>;
}

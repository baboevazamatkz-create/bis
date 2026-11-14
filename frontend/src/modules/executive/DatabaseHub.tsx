import { useEffect, useRef, useState } from 'react';
import { activeProject, api, fetchBlob, type User } from '../../api';
import { ConfirmButton, PageHead } from '../../components';

type Level = 'knowledge' | 'project' | 'organization';
interface Connection { id: string; name: string; level: Level; path: string; object_id: string | null; enabled: boolean; status: string; error: string | null; synced_at: string | null }
interface State { connections: Connection[]; allowed_roots: string[]; platform_admin: boolean; strict: boolean }
interface FileRow { id: string; path: string; state: string; error: string | null; source_id?: string; source_kind?: string; source_status?: string; active_version_id?: string;
  versions?: { id: string; form_kind: string | null; approved_at: string | null }[] }
type AIProvider = 'anthropic' | 'openai' | 'ollama' | 'compatible' | 'gigachat' | 'yandex';
interface ReadingStatus { key_configured?: boolean; provider_key_configured?: boolean; selected_provider?: string | null; last_model_attempts?: {provider: string; ok: boolean; error?: string; checked_at: string}[]; providers: { name: string; capabilities: string[] }[]; model_error: string | null; text_configured: boolean; vision_configured: boolean; ocr_configured: boolean; folder_id: string | null; local_setup_available: boolean; voice: { stt: boolean } }
interface ModelCheck { providers: { provider: string; ok: boolean; error?: string; hint?: string; reply?: string }[] }
interface Imports { total: number; documents: { id: string; title: string; state: string; error: string | null }[]; has_more: boolean }
const LABELS = { knowledge: 'База знаний', project: 'База проекта', organization: 'База организаций' };
const STATES: Record<string,string> = { ready: 'Текст доступен', partial: 'Есть ошибки чтения', pending: 'Подключена, ожидает чтения', reading: 'Читаю документы…', error: 'Ошибка', empty: 'Очищена', removed: 'Удалён из папки' };

export default function DatabaseHub({ user }: { user: User }) {
  const filePicker = useRef<HTMLInputElement | null>(null);
  const folderPicker = useRef<HTMLInputElement | null>(null);
  const [level, setLevel] = useState<Level>('project');
  const [state, setState] = useState<State | null>(null);
  const [reading, setReading] = useState<ReadingStatus | null>(null);
  const [files, setFiles] = useState<Record<string,FileRow[]>>({});
  const [imports, setImports] = useState<Partial<Record<Level,Imports>>>({});
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState<Record<string, string>>({});
  const running = useRef(new Set<string>());
  const busy = !!pending.database;
  const refreshing = useRef(false);
  const configRevision = useRef(0);
  const checks = useRef(new Set<AbortController>());
  const configureOpen = useRef(false);
  const mounted = useRef(true);
  const [connecting, setConnecting] = useState(false);
  const [path, setPath] = useState('');
  const [folder, setFolder] = useState('');
  const [visionModel, setVisionModel] = useState('');
  const [provider, setProvider] = useState<AIProvider>('anthropic');
  const [providerModel, setProviderModel] = useState('claude-opus-5-5');
  const [providerUrl, setProviderUrl] = useState('');
  const [providerKey, setProviderKey] = useState('');
  const [gigachatScope, setGigachatScope] = useState('GIGACHAT_API_CORP');
  const [configure, setConfigure] = useState(false);
  const [check, setCheck] = useState<ModelCheck | null>(null);
  const [unassigned, setUnassigned] = useState<{ id: string; title: string }[]>([]);
  const [formReview, setFormReview] = useState<{ id: string; kind: string; mapping: Record<string,unknown>; format: string; previewed: boolean } | null>(null);
  const canWrite = user.role === 'admin';
  async function load(signal?: AbortSignal) {
    if (refreshing.current) return;
    refreshing.current = true;
    const revision = configRevision.current;
    try {
      const result = await api<State>('/libraries', { signal });
      if (signal?.aborted || !mounted.current) return;
      setState(result);
      const jobs: Promise<unknown>[] = [
        api<ReadingStatus>('/workspace/reading-status', { signal, timeoutMs: 8000 }).then((status) => {
          if (signal?.aborted || !mounted.current || revision !== configRevision.current) return;
          setReading(status); setFolder((old) => old || status.folder_id || '');
          const selected: Record<string,AIProvider> = { 'anthropic-claude': 'anthropic', openai: 'openai', ollama: 'ollama', 'custom-model': 'compatible', gigachat: 'gigachat', yandexgpt: 'yandex' };
          if (!configureOpen.current && status.selected_provider && selected[status.selected_provider]) setProvider(selected[status.selected_provider]);
        }),
        ...result.connections.filter((c) => c.level === level && (c.level !== 'project' || c.object_id === activeProject())).map(async (c) => {
          const rows = await api<FileRow[]>(`/libraries/${c.id}/files`, { signal });
          if (!signal?.aborted && mounted.current) setFiles((old) => ({ ...old, [c.id]: rows }));
        }),
      ];
      if (canWrite) jobs.push(api<typeof unassigned>('/libraries/unassigned/list', { signal }).then((rows) => {
        if (!signal?.aborted && mounted.current) setUnassigned(rows);
      }));
      if (level !== 'knowledge' && (level !== 'project' || activeProject())) jobs.push(
        api<Imports>(`/workspace/imports?level=${level}`, { signal }).then((rows) => {
          if (!signal?.aborted && mounted.current) setImports((old) => ({ ...old, [level]: rows }));
        }));
      const results = await Promise.allSettled(jobs);
      const failed = results.find((r) => r.status === 'rejected');
      if (failed?.status === 'rejected') throw failed.reason;
    } finally { refreshing.current = false; }
  }
  function refresh() { void load().catch((e) => { if (mounted.current) setError(`Не удалось обновить сведения: ${e.message}`); }); }
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (refreshing.current) { timer = setTimeout(poll, 100); return; }
      try { await load(controller.signal); }
      catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 8000);
    };
    void poll();
    return () => { mounted.current = false; controller.abort(); clearTimeout(timer); };
  }, [level]);
  async function action(fn: () => Promise<unknown>, success: string, scope = 'database', label = 'Обрабатываю базу…') {
    if (running.current.has(scope)) return;
    running.current.add(scope); setPending((old) => ({ ...old, [scope]: label }));
    setError(''); setMessage('');
    try { await fn(); if (mounted.current) { setMessage(success); refresh(); } }
    catch (e) { if (mounted.current) setError((e as Error).message); }
    finally {
      running.current.delete(scope);
      if (mounted.current) setPending((old) => { const next = { ...old }; delete next[scope]; return next; });
    }
  }
  async function checkCurrent<T>(run: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const controller = new AbortController(); checks.current.add(controller);
    const revision = configRevision.current;
    try { return await run(controller.signal); }
    finally { checks.current.delete(controller); if (revision !== configRevision.current) throw new Error('Настройки изменились во время проверки. Повторите проверку с новым подключением.'); }
  }
  async function uploadFiles(list: FileList | null) {
    if (!list?.length) return;
    const selected = Array.from(list);
    await action(async () => {
      const form = new FormData(); form.append('level', level);
      if (level === 'project') form.append('object_id', activeProject()!);
      selected.forEach((file) => form.append('files', file, file.webkitRelativePath || file.name));
      await api('/libraries/upload', { method: 'POST', body: form, timeoutMs: 600_000 });
    }, 'Файлы загружены. Началось чтение базы.');
    if (filePicker.current) filePicker.current.value = '';
    if (folderPicker.current) folderPicker.current.value = '';
  }
  const connections = state?.connections.filter((c) => c.level === level && (level !== 'project' || c.object_id === activeProject())) ?? [];
  const allFiles = connections.flatMap((c) => files[c.id] ?? []);
  return <>
    <PageHead title="Базы данных" hint="Подключите свои папки. Синапс читает эти документы и использует их в ответах и заданиях." />
    {(error || message) && <div className="workspace-feedback">{error && <div className="alert error" role="alert">{error}</div>}{message && <div className="note" role="status">{message}</div>}</div>}
    <div className="row"><p className="small muted">{state?.strict ? 'Ответы и формы — только из подключённых баз и документов активного проекта.' : 'Строгий режим источников выключен.'}</p>
      {canWrite && state && <button className="btn small" disabled={busy} onClick={() => void action(() => api('/libraries/policy', { method: 'PUT', json: { strict: !state.strict } }), 'Режим источников обновлён.')}>{state.strict ? 'Изменить режим источников' : 'Использовать только мои базы'}</button>}</div>
    <div className="database-tabs">{(Object.entries(LABELS) as [Level,string][]).map(([id,label]) => <button key={id} className={`database-tab ${level === id ? 'active' : ''}`} onClick={() => { setLevel(id); setConnecting(false); }}>{label}</button>)}</div>
    <div className="panel">
      <div className="section-list-header row"><div><h2>{LABELS[level]}</h2><p className="small muted">{level === 'knowledge' ? 'Нормативы, стандарты, шаблоны и формы.' : level === 'project' ? 'РД, журналы, акты, схемы, сертификаты и договоры активного объекта.' : 'Реквизиты, регламенты и документы вашей организации.'}</p></div>
        {canWrite && <button className="btn primary" disabled={level === 'project' && !activeProject() || level === 'knowledge' && !state?.platform_admin} onClick={() => setConnecting(!connecting)}>Подключить базу</button>}</div>
      {level === 'project' && !activeProject() && <p className="note">Сначала выберите объект вверху страницы.</p>}
      {canWrite && <div className="row">
        <input hidden type="file" multiple ref={filePicker} onChange={(e) => void uploadFiles(e.target.files)} />
        <input hidden type="file" multiple ref={(node) => { folderPicker.current = node; node?.setAttribute('webkitdirectory', ''); }} onChange={(e) => void uploadFiles(e.target.files)} />
        <button className="btn" disabled={busy || level === 'project' && !activeProject() || level === 'knowledge' && !state?.platform_admin} onClick={() => filePicker.current?.click()}>Загрузить файлы</button>
        <button className="btn" disabled={busy || level === 'project' && !activeProject() || level === 'knowledge' && !state?.platform_admin} onClick={() => folderPicker.current?.click()}>Загрузить папку</button>
        {busy && <span role="status">{pending.database}</span>}
      </div>}
      {level === 'knowledge' && state && !state.platform_admin && <p className="note">Общая база знаний доступна для изменения администратору платформы.</p>}
      {connecting && <div className="connection-editor"><label htmlFor="database-path">Папка на компьютере с BIS</label><div className="row">
        <input id="database-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder="C:\BIS-Pilot\library\project-01" />
        <button className="btn primary" disabled={busy || !path.trim()} onClick={() => void action(async () => {
          await api('/libraries', { method: 'POST', json: { name: path.split(/[\\/]/).filter(Boolean).slice(-1)[0] || LABELS[level], path, level, object_id: level === 'project' ? activeProject() : null } });
          setConnecting(false); setPath('');
        }, 'Папка подключена. Началось чтение файлов.')}>Подключить и прочитать</button></div>
        <p className="small muted">Разрешённые каталоги: {state?.allowed_roots.join(', ') || 'не настроены'}</p></div>}
      <p className="small muted">Файлов: {allFiles.length} · прочитано: {allFiles.filter((f) => f.state === 'ready').length} · ошибок: {allFiles.filter((f) => f.state === 'error').length}</p>
      {!connections.length && <p className="muted">База пока не подключена.</p>}
      {connections.map((c) => <div className="database-connection" key={c.id}>
        <div className="row section-list-header"><div><strong>{c.name}</strong><p className="small muted">{c.path}</p><span className="small">{c.enabled ? STATES[c.status] ?? c.status : c.status === 'empty' ? 'Очищена' : 'Отключена'}</span></div>
          {canWrite && (c.level !== 'knowledge' || state?.platform_admin) && <div className="row">
            <button className="btn small" disabled={busy} onClick={() => void action(async () => {
              if (!c.enabled) await api(`/libraries/${c.id}?enabled=true`, { method: 'PATCH' });
              await api(`/libraries/${c.id}/sync?force=true`, { method: 'POST' });
            }, 'Запущено полное перечитывание базы, включая неизменившиеся файлы.')}>Обновить базу</button>
            <ConfirmButton disabled={busy} label="Очистить базу" confirmLabel="Удалить данные этой базы?" onConfirm={() => void action(() => api(`/libraries/${c.id}/contents`, { method: 'DELETE' }), 'База очищена и отключена. Оригиналы в папке сохранены.')} />
          </div>}
        </div>
        {c.error && <p className="small text-bad">{c.error}</p>}
        <details><summary>Файлы и ошибки чтения · {files[c.id]?.length ?? 0}</summary><div className="database-files">{files[c.id]?.map((f) => <div key={f.id}><strong className="small">{f.path}</strong><span className={f.state === 'error' ? 'text-bad small' : 'muted small'}>{STATES[f.state] ?? f.state}</span>{f.error && <p className="small text-bad">{f.error}</p>}
          {canWrite && (c.level !== 'knowledge' || state?.platform_admin) && <div className="row">
            {f.source_kind === 'norm' && f.source_status === 'unknown' && <button className="btn small" disabled={busy} onClick={() => void action(() => api(`/knowledge/sources/${f.source_id}`, { method: 'PATCH', json: { status: 'active' } }), 'Редакция норматива подтверждена.')}>Подтвердить действующую редакцию</button>}
            {f.versions?.filter((v) => v.id === f.active_version_id && v.form_kind).map((v) => <button key={v.id} className="btn small" disabled={busy} onClick={() => void action(async () => {
              const mapping = await api<Record<string,unknown>>(`/libraries/versions/${v.id}/prepare`, { method: 'POST' });
              setFormReview({ id: v.id, kind: v.form_kind!, mapping, format: String(mapping.format), previewed: false });
            }, 'Откройте пробное заполнение и проверьте форму.')}>{v.approved_at ? 'Проверить форму' : 'Подтвердить форму'}</button>)}
          </div>}
        </div>)}</div></details>
      </div>)}
      <p className="small muted">Очистка удаляет записи выбранного подключения из BIS и отключает автоматическую синхронизацию. Исходные файлы на диске сохраняются.</p>
    </div>
    {!!imports[level]?.total && <div className="panel"><div className="row section-list-header"><h2>Загруженные файлы · {imports[level]?.total}</h2>
      {canWrite && <div className="row"><button className="btn small" disabled={busy} onClick={() => void action(() => api(`/workspace/imports/reread?level=${level}`, { method: 'POST' }), 'Началось перечитывание сохранённых оригиналов. Повторно загружать файлы не нужно.')}>Перечитать загруженные файлы</button>
        <ConfirmButton disabled={busy} label="Очистить загруженные файлы" confirmLabel="Удалить импортированные данные этой базы?" onConfirm={() => void action(() => api(`/workspace/imports?level=${level}`, { method: 'DELETE' }), 'Импортированные данные очищены. Созданные в BIS акты сохранены.')} /></div>}
      </div><details><summary>Состояние чтения</summary><div className="database-files">{imports[level]?.documents.map((d) => <div key={d.id}><strong className="small">{d.title}</strong><span className="small muted">{({ stored: 'Оригинал загружен', queued: 'Ожидает перечитывания', running: 'Читаю…', ready: 'Текст перечитан', error: 'Ошибка чтения' } as Record<string,string>)[d.state] ?? d.state}</span>{d.error && <p className="small text-bad">{d.error}</p>}</div>)}</div></details>
      {imports[level]?.has_more && <p className="small muted">Здесь показаны первые 200 файлов. Перечитывание применяется ко всем {imports[level]?.total} файлам этой базы.</p>}
    </div>}
    {canWrite && unassigned.length > 0 && <details className="panel"><summary>Нужно привязать документы · {unassigned.length}</summary><p className="small muted">Эти файлы ещё не назначены проекту или организации. Выберите, где их использовать.</p>
      {unassigned.map((d) => <div className="database-connection" key={d.id}><strong>{d.title}</strong><div className="row">
        <button className="btn small" disabled={busy || !activeProject()} onClick={() => void action(() => api(`/libraries/unassigned/${d.id}`, { method: 'POST', json: { level: 'project', object_id: activeProject() } }), 'Документ назначен активному проекту.')}>Для этого проекта</button>
        <button className="btn small" disabled={busy} onClick={() => void action(() => api(`/libraries/unassigned/${d.id}`, { method: 'POST', json: { level: 'organization' } }), 'Документ назначен организации.')}>Для организации</button></div></div>)}
    </details>}
    {formReview && <div className="panel"><h2>Проверка формы</h2><p className="small muted">Скачайте пробное заполнение и проверьте расположение данных. Ручное заполнение полей акта не требуется.</p><div className="row">
      <button className="btn" disabled={busy} onClick={() => void action(async () => {
        const blob = await fetchBlob(`/libraries/versions/${formReview.id}/preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: formReview.kind, mapping: formReview.mapping }) });
        const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `Проверка_формы.${formReview.format.startsWith('pdf') ? 'pdf' : formReview.format}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
        setFormReview({ ...formReview, previewed: true });
      }, 'Пробное заполнение скачано.')}>Скачать пробное заполнение</button>
      <button className="btn primary" disabled={busy || !formReview.previewed} onClick={() => void action(async () => { await api(`/libraries/versions/${formReview.id}/approve`, { method: 'POST', json: { kind: formReview.kind, mapping: formReview.mapping } }); setFormReview(null); }, 'Форма подтверждена.')}>Проверено, использовать форму</button>
      <button className="btn" onClick={() => setFormReview(null)}>Закрыть</button>
    </div></div>}
    <details className="panel"><summary>ИИ и качество чтения</summary>
      {Object.entries(pending).filter(([id]) => id !== 'database').map(([id, label]) => <p role="status" key={id}>{label}</p>)}
      <p className="small">Языковая модель: {!reading ? 'получаю состояние…' : reading.text_configured ? 'настройки сохранены' : 'не настроена'} · OCR: {!reading ? 'получаю состояние…' : reading.ocr_configured ? 'настройки сохранены' : 'не настроен'} · чтение чертежей зрением: {reading?.vision_configured ? 'настроено' : 'не подключено'} · голос: {reading?.voice.stt ? 'настроен' : 'не подключён'}</p>
      {reading && <p className="small muted">Текущая языковая модель: {reading.selected_provider || 'не выбрана'} · {reading.selected_provider === 'ollama' ? 'ключ не требуется' : `ключ ${reading.provider_key_configured ? 'сохранён' : 'не задан'}`}. OCR: {reading.key_configured ? 'ключ Яндекса задан' : 'ключ Яндекса не задан'}.</p>}
      {reading?.last_model_attempts?.map((attempt) => <p className={attempt.ok ? 'small muted' : 'small text-bad'} key={attempt.provider}>{attempt.provider}: {attempt.ok ? 'последний запрос выполнен' : `ошибка последнего запроса — ${attempt.error}`}</p>)}
      {reading?.model_error && <p className="text-bad">{reading.model_error}</p>}
      {canWrite && <div className="row">{(pending['model-check'] || pending['ocr-check']) && <button className="btn" onClick={() => checks.current.forEach((c) => c.abort())}>Остановить проверки</button>}<button className="btn" disabled={!!pending['model-check'] || !!pending['ai-save']} onClick={() => void action(async () => setCheck(await checkCurrent((signal) => api<ModelCheck>('/ai/models/check', { method: 'POST', signal, timeoutMs: 130_000 }))), 'Проверка моделей завершена.', 'model-check', 'Проверяю языковую модель (до 130 секунд)…')}>Проверить ИИ</button>
        <button className="btn" disabled={!!pending['ocr-check'] || !!pending['ai-save']} onClick={() => void action(async () => { const result = await checkCurrent((signal) => api<{text: string}>('/workspace/ocr-check', { method: 'POST', signal, timeoutMs: 130_000 })); if (!result.text.trim()) throw new Error('OCR не извлёк текст контрольного изображения'); }, 'OCR прочитал контрольное изображение. Можно перечитать сканы.', 'ocr-check', 'Проверяю OCR (до 130 секунд)…')}>Проверить OCR</button>
        {(!reading || reading.local_setup_available) && <button className="btn" onClick={() => setConfigure((old) => { configureOpen.current = !old; return !old; })}>Выбрать языковую модель</button>}</div>}
      {check?.providers.length === 0 && <p className="text-bad">Настроен только офлайн-режим.</p>}
      {check?.providers.map((p) => <div className="note" key={p.provider}><strong>{p.provider}: {p.ok ? 'Ответ получен' : 'Ошибка подключения'}</strong><p className="small">{p.error || p.reply}</p>{p.hint && <p className="small text-bad">{p.hint}</p>}</div>)}
      {configure && <div className="connection-editor"><p className="small muted">Выберите языковую модель. Ключ сохранится локально в настройках BIS. OCR и распознавание голоса останутся подключены к текущим отдельным сервисам.</p>
        <label htmlFor="ai-provider">Провайдер</label><select id="ai-provider" value={provider} onChange={(e) => { const value = e.target.value as AIProvider; setProvider(value); setProviderModel(({ anthropic: 'claude-opus-5-5', openai: 'gpt-4.1-mini', ollama: 'qwen3:8b', compatible: '', gigachat: 'GigaChat-2-Max', yandex: 'yandexgpt/latest' } as Record<AIProvider,string>)[value]); setProviderUrl(value === 'ollama' ? 'http://127.0.0.1:11434/v1' : ''); }}>
          <option value="anthropic">Anthropic · Claude</option><option value="openai">OpenAI</option><option value="ollama">Ollama · на этом компьютере</option><option value="compatible">Другой OpenAI‑совместимый API</option><option value="gigachat">GigaChat</option><option value="yandex">YandexGPT</option>
        </select>
        <label htmlFor="ai-model">Модель</label><input id="ai-model" value={providerModel} onChange={(e) => setProviderModel(e.target.value)} placeholder={provider === 'anthropic' ? 'claude-opus-5-5' : 'Идентификатор модели'} />
        {(provider === 'ollama' || provider === 'compatible' || provider === 'gigachat') && <><label htmlFor="ai-url">Адрес API {provider === 'ollama' ? 'Ollama' : '(если отличается от стандартного)'}</label><input id="ai-url" value={providerUrl} onChange={(e) => setProviderUrl(e.target.value)} placeholder={provider === 'ollama' ? 'http://127.0.0.1:11434/v1' : provider === 'compatible' ? 'https://api.example.com/v1' : 'Стандартный адрес GigaChat'} /></>}
        {provider === 'yandex' && <><label htmlFor="ai-folder">Folder ID</label><input id="ai-folder" value={folder} onChange={(e) => setFolder(e.target.value)} /></>}
        {provider === 'gigachat' && <><label htmlFor="gigachat-scope">Тип доступа</label><select id="gigachat-scope" value={gigachatScope} onChange={(e) => setGigachatScope(e.target.value)}><option value="GIGACHAT_API_CORP">Для бизнеса</option><option value="GIGACHAT_API_B2B">B2B</option><option value="GIGACHAT_API_PERS">Для физического лица</option></select></>}
        {provider !== 'ollama' && <><label htmlFor="ai-provider-key">API‑ключ (пусто — оставить сохранённый)</label><input id="ai-provider-key" type="password" autoComplete="new-password" value={providerKey} onChange={(e) => setProviderKey(e.target.value)} /></>}
        {provider === 'yandex' && <details><summary>Модель для изображений и чертежей</summary><p className="small muted">Для визуального чтения чертежей укажите доступную вашему аккаунту модель из AI Studio. OCR распознаёт надписи отдельно.</p><input aria-label="Модель зрения" value={visionModel} onChange={(e) => setVisionModel(e.target.value)} placeholder="Идентификатор модели/версия" /></details>}
        <button className="btn primary" disabled={!!pending['ai-save'] || !providerModel.trim() || provider === 'yandex' && !folder.trim() || provider === 'compatible' && !providerUrl.trim()} onClick={() => void action(async () => {
          await api('/workspace/ai-provider', { method: 'POST', json: { provider, model: providerModel.trim(), api_key: providerKey || null, base_url: providerUrl.trim() || null, folder_id: provider === 'yandex' ? folder.trim() : null, scope: gigachatScope } });
          configRevision.current += 1; setProviderKey(''); configureOpen.current = false; setConfigure(false); setCheck(null);
          setReading((old) => old ? { ...old, selected_provider: ({ anthropic: 'anthropic-claude', openai: 'openai', ollama: 'ollama', compatible: 'custom-model', gigachat: 'gigachat', yandex: 'yandexgpt' } as Record<AIProvider,string>)[provider], provider_key_configured: provider !== 'ollama' || old.provider_key_configured, last_model_attempts: [] } : old);
        }, 'Выбор языковой модели сохранён. Нажмите «Проверить ИИ». OCR и голосовые настройки не менялись.', 'ai-save', 'Сохраняю подключение ИИ…')}>{provider === 'anthropic' ? 'Подключить Claude' : 'Сохранить и выбрать модель'}</button>
      </div>}
    </details>
  </>;
}

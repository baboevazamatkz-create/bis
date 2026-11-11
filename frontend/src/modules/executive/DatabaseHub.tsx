import { useEffect, useState } from 'react';
import { activeProject, api, fetchBlob, type User } from '../../api';
import { ConfirmButton, PageHead } from '../../components';

type Level = 'knowledge' | 'project' | 'organization';
interface Connection { id: string; name: string; level: Level; path: string; object_id: string | null; enabled: boolean; status: string; error: string | null; synced_at: string | null }
interface State { connections: Connection[]; allowed_roots: string[]; platform_admin: boolean; strict: boolean }
interface FileRow { id: string; path: string; state: string; error: string | null; source_id?: string; source_kind?: string; source_status?: string; active_version_id?: string;
  versions?: { id: string; form_kind: string | null; approved_at: string | null }[] }
interface ReadingStatus { providers: { name: string; capabilities: string[] }[]; model_error: string | null; text_configured: boolean; vision_configured: boolean; ocr_configured: boolean; folder_id: string | null; local_setup_available: boolean; voice: { stt: boolean } }
interface ModelCheck { providers: { provider: string; ok: boolean; error?: string; hint?: string; reply?: string }[] }
interface Imports { total: number; documents: { id: string; title: string; state: string; error: string | null }[]; has_more: boolean }
const LABELS = { knowledge: 'База знаний', project: 'База проекта', organization: 'База организаций' };
const STATES: Record<string,string> = { ready: 'Текст доступен', partial: 'Есть ошибки чтения', pending: 'Подключена, ожидает чтения', reading: 'Читаю документы…', error: 'Ошибка', empty: 'Очищена', removed: 'Удалён из папки' };

export default function DatabaseHub({ user }: { user: User }) {
  const [level, setLevel] = useState<Level>('project');
  const [state, setState] = useState<State | null>(null);
  const [reading, setReading] = useState<ReadingStatus | null>(null);
  const [files, setFiles] = useState<Record<string,FileRow[]>>({});
  const [imports, setImports] = useState<Partial<Record<Level,Imports>>>({});
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [path, setPath] = useState('');
  const [folder, setFolder] = useState('');
  const [key, setKey] = useState('');
  const [visionModel, setVisionModel] = useState('');
  const [configure, setConfigure] = useState(false);
  const [check, setCheck] = useState<ModelCheck | null>(null);
  const [unassigned, setUnassigned] = useState<{ id: string; title: string }[]>([]);
  const [formReview, setFormReview] = useState<{ id: string; kind: string; mapping: Record<string,unknown>; format: string; previewed: boolean } | null>(null);
  const canWrite = user.role === 'admin';
  async function load() {
    const result = await api<State>('/libraries'); setState(result);
    const status = await api<ReadingStatus>('/workspace/reading-status'); setReading(status);
    setFolder((old) => old || status.folder_id || '');
    const groups = await Promise.all(result.connections.filter((c) => c.level !== 'project' || c.object_id === activeProject()).map(async (c) =>
      [c.id, await api<FileRow[]>(`/libraries/${c.id}/files`)] as const));
    setFiles(Object.fromEntries(groups));
    if (canWrite) setUnassigned(await api('/libraries/unassigned/list'));
    if (level !== 'knowledge' && (level !== 'project' || activeProject())) {
      const uploaded = await api<Imports>(`/workspace/imports?level=${level}`);
      setImports((old) => ({ ...old, [level]: uploaded }));
    }
  }
  useEffect(() => { void load().catch((e) => setError(e.message)); const timer = setInterval(() => void load().catch(() => {}), 8000); return () => clearInterval(timer); }, [level]);
  async function action(fn: () => Promise<unknown>, success: string) {
    setBusy(true); setError(''); setMessage('');
    try { await fn(); await load(); setMessage(success); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const connections = state?.connections.filter((c) => c.level === level && (level !== 'project' || c.object_id === activeProject())) ?? [];
  const allFiles = connections.flatMap((c) => files[c.id] ?? []);
  return <>
    <PageHead title="Базы данных" hint="Подключите свои папки. Синапс читает эти документы и использует их в ответах и заданиях." />
    {error && <div className="alert error" role="alert">{error}</div>}{message && <div className="note">{message}</div>}
    <div className="row"><p className="small muted">{state?.strict ? 'Ответы и формы — только из подключённых баз и документов активного проекта.' : 'Строгий режим источников выключен.'}</p>
      {canWrite && state && <button className="btn small" disabled={busy} onClick={() => void action(() => api('/libraries/policy', { method: 'PUT', json: { strict: !state.strict } }), 'Режим источников обновлён.')}>{state.strict ? 'Изменить режим источников' : 'Использовать только мои базы'}</button>}</div>
    <div className="database-tabs">{(Object.entries(LABELS) as [Level,string][]).map(([id,label]) => <button key={id} className={`database-tab ${level === id ? 'active' : ''}`} onClick={() => { setLevel(id); setConnecting(false); }}>{label}</button>)}</div>
    <div className="panel">
      <div className="section-list-header row"><div><h2>{LABELS[level]}</h2><p className="small muted">{level === 'knowledge' ? 'Нормативы, стандарты, шаблоны и формы.' : level === 'project' ? 'РД, журналы, акты, схемы, сертификаты и договоры активного объекта.' : 'Реквизиты, регламенты и документы вашей организации.'}</p></div>
        {canWrite && <button className="btn primary" disabled={level === 'project' && !activeProject() || level === 'knowledge' && !state?.platform_admin} onClick={() => setConnecting(!connecting)}>Подключить базу</button>}</div>
      {level === 'project' && !activeProject() && <p className="note">Сначала выберите объект вверху страницы.</p>}
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
            <ConfirmButton label="Очистить базу" confirmLabel="Удалить данные этой базы?" onConfirm={() => void action(() => api(`/libraries/${c.id}/contents`, { method: 'DELETE' }), 'База очищена и отключена. Оригиналы в папке сохранены.')} />
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
        <ConfirmButton label="Очистить загруженные файлы" confirmLabel="Удалить импортированные данные этой базы?" onConfirm={() => void action(() => api(`/workspace/imports?level=${level}`, { method: 'DELETE' }), 'Импортированные данные очищены. Созданные в BIS акты сохранены.')} /></div>}
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
      <p className="small">Языковая модель: {reading?.text_configured ? 'настроена, требуется проверка доступа' : 'не подключена'} · OCR: {reading?.ocr_configured ? 'настроен' : 'не подключён'} · чтение чертежей зрением: {reading?.vision_configured ? 'настроено' : 'не подключено'} · голос: {reading?.voice.stt ? 'настроен' : 'не подключён'}</p>
      {reading?.model_error && <p className="text-bad">{reading.model_error}</p>}
      {canWrite && <div className="row"><button className="btn" disabled={busy} onClick={() => void action(async () => setCheck(await api<ModelCheck>('/ai/models/check', { method: 'POST', signal: AbortSignal.timeout(130_000) })), 'Проверка моделей завершена.')}>Проверить ИИ</button>
        {reading?.local_setup_available && <button className="btn" onClick={() => setConfigure(!configure)}>Подключить ИИ и OCR</button>}</div>}
      {check?.providers.length === 0 && <p className="text-bad">Настроен только офлайн-режим.</p>}
      {check?.providers.map((p) => <div className="note" key={p.provider}><strong>{p.provider}: {p.ok ? 'Ответ получен' : 'Ошибка подключения'}</strong><p className="small">{p.error || p.reply}</p>{p.hint && <p className="small text-bad">{p.hint}</p>}</div>)}
      {configure && <div className="connection-editor"><p className="small muted">Яндекс Cloud получит обрабатываемые фрагменты и изображения. Нужны права на языковые модели, OCR и SpeechKit.</p>
        <label htmlFor="ai-folder">Folder ID</label><input id="ai-folder" value={folder} onChange={(e) => setFolder(e.target.value)} />
        <label htmlFor="ai-key">API-ключ (пусто — сохранить текущий)</label><input id="ai-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
        <details><summary>Модель для изображений и чертежей</summary><p className="small muted">OCR распознаёт надписи. Для визуального чтения чертежей укажите доступную вашему аккаунту модель из AI Studio. Пустое поле сохраняет ранее настроенные модели зрения.</p><input aria-label="Модель зрения" value={visionModel} onChange={(e) => setVisionModel(e.target.value)} placeholder="Идентификатор модели/версия" /></details>
        <button className="btn primary" disabled={busy || !folder.trim()} onClick={() => void action(async () => {
          await api('/workspace/ai-setup', { method: 'POST', json: { folder_id: folder.trim(), api_key: key || null, ocr: true, speech: true, vision_model: visionModel.trim() || null } });
          setKey(''); setConfigure(false);
        }, 'ИИ, OCR и голос настроены. Проверьте ответ модели и перечитайте базу.')}>Сохранить подключение</button>
      </div>}
    </details>
  </>;
}

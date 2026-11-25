import { useEffect, useRef, useState, type ReactNode } from 'react';
import { activeProject, api, download, fetchFile, openFile } from '../../api';
import './DocumentFormReview.css';

type FieldSource = 'auto' | 'registry' | 'project' | 'documents' | 'manual';
export interface FormEntry { field: string; target: string; anchor: string; mode: string; source?: FieldSource }
type LayoutNode = { type: 'paragraph'; id: string; text: string } | { type: 'table'; rows: LayoutNode[][][] };
export interface PreparedForm {
  format: string;
  profile?: string | null;
  entries: FormEntry[];
  targets: Record<string, string>;
  fields?: Record<string, string>;
  origins?: Record<string, string>;
  modes?: Record<string, string>;
  target_labels?: Record<string, string>;
  required?: string[];
  alternatives?: Record<string, string[]>;
  example?: boolean;
  layout?: LayoutNode[];
}
export interface FormReview { id: string; name: string; kind: string; mapping: PreparedForm; approved?: boolean }

interface Candidate { id: string; kind: FieldSource; label: string; value: string; document_id?: string; page?: number; quote?: string }
interface ResolvedField {
  field: string;
  state: 'resolved' | 'missing' | 'conflict';
  value: string | null;
  sources: Candidate[];
  issues?: string[];
}
interface Resolution { fields: Record<string, ResolvedField> }

const formKinds: Record<string, string> = { aosr: 'АОСР', aook: 'АООК', letter: 'Письмо', work_journal: 'Журнал работ', id_registry: 'Реестр ИД' };
const sourceLabels: Record<FieldSource, string> = {
  auto: 'Выбрать по доступным данным',
  registry: 'Карточки и структура проекта',
  project: 'Факты проекта',
  documents: 'Документы',
  manual: 'Вручную',
};

function missingFields(mapping: PreparedForm) {
  const mapped = new Set(mapping.entries.map((entry) => entry.field));
  return (mapping.required ?? []).filter((field) => !mapped.has(field) &&
    !mapping.alternatives?.[field]?.every((alternative) => mapped.has(alternative)));
}

function sourcePosition(mapping: PreparedForm, target: string) {
  const [paragraph, start, end] = target.split(':').map(Number);
  const anchor = mapping.targets[target] ?? '';
  const label = mapping.target_labels?.[target];
  if (label && !anchor) return `Пустая ячейка · ${label}`;
  const excerpt = mapping.example && Number.isFinite(start) && Number.isFinite(end)
    ? Array.from(anchor).slice(start, end).join('')
    : anchor;
  const position = Number.isFinite(start) ? `, пропуск с позиции ${start + 1}` : '';
  return `Абзац ${paragraph + 1}${position} · ${label || excerpt.trim() || 'пустой абзац'}`;
}

function saveFile(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function resultNotes(headers: Headers): string[] {
  const raw = headers.get('X-BIS-Document-Issues');
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(raw));
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((item: unknown) => {
      if (typeof item === 'string') return [item];
      if (item && typeof item === 'object' && 'message' in item && typeof item.message === 'string') return [item.message];
      return [];
    });
  } catch { return []; }
}

function SourceEvidence({ source, onOpen }: { source: Candidate; onOpen: (source: Candidate) => void }) {
  return <div className="doc-form-evidence">
    <span className="small muted">{source.label}{source.page ? ` · стр. ${source.page}` : ''}</span>
    {source.quote && <blockquote>{source.quote}</blockquote>}
    {source.document_id && <button type="button" className="link small" onClick={() => onOpen(source)}>Открыть источник</button>}
  </div>;
}

function DocumentLayout({ mapping, selected, onSelect }: {
  mapping: PreparedForm; selected: string | null; onSelect: (target: string) => void;
}) {
  const positions = new Map<string, { target: string; start: number; end: number }[]>();
  for (const target of Object.keys(mapping.targets)) {
    const [paragraph, start, end] = target.split(':');
    if (start === undefined || end === undefined) continue;
    const ranges = positions.get(paragraph) ?? [];
    ranges.push({ target, start: Number(start), end: Number(end) });
    positions.set(paragraph, ranges);
  }
  function mark(text: string, target: string, wholeParagraph = false) {
    const mapped = mapping.entries.find((entry) => entry.target === target);
    const origin = mapping.origins?.[target];
    const state = origin === 'model' ? ' check' : mapped ? ' mapped' : ' unmapped';
    const className = 'doc-form-marker' + state + (selected === target ? ' selected' : '') + (wholeParagraph ? ' whole' : '');
    const field = mapped ? mapping.fields?.[mapped.field] : undefined;
    return <button key={target} type="button" className={className} aria-pressed={selected === target}
      aria-label={field ? `${field} · ${sourcePosition(mapping, target)}` : sourcePosition(mapping, target)}
      onClick={() => onSelect(target)}>{text || 'Пустое место'}</button>;
  }

  function draw(nodes: LayoutNode[], path = 'root'): ReactNode {
    return nodes.map((node, index) => {
      const key = `${path}:${index}`;
      if (node.type === 'table') return <div key={key} className="doc-form-table"><table><tbody>
        {node.rows.map((row, rowIndex) => <tr key={rowIndex}>
          {row.map((cell, colIndex) => <td key={colIndex}>{draw(cell, `${key}:${rowIndex}:${colIndex}`)}</td>)}
        </tr>)}
      </tbody></table></div>;
      const chars = Array.from(node.text);
      const spans = (positions.get(node.id) ?? []).sort((a, b) => a.start - b.start);
      if (!spans.length) {
        const entry = mapping.entries.find((entry) => entry.target === node.id);
        const mode = entry?.mode ?? mapping.modes?.[node.id];
        const slot = (mode === 'slot' ? /_{2,}/ : mode === 'token' ? /\{\{\s*[\p{L}\p{N}_]+\s*\}\}/u : null)?.exec(node.text);
        if (slot) return <p key={key}>{node.text.slice(0, slot.index)}{mark(slot[0], node.id)}{node.text.slice(slot.index + slot[0].length)}</p>;
        const selectable = entry || Object.prototype.hasOwnProperty.call(mapping.target_labels ?? {}, node.id);
        return <p key={key}>{selectable ? mark(node.text, node.id, true) : node.text}</p>;
      }
      let cursor = 0;
      const pieces: ReactNode[] = [];
      for (const { target, start, end } of spans) {
        if (start < cursor || end < start || end > chars.length) continue;
        if (start > cursor) pieces.push(chars.slice(cursor, start).join(''));
        pieces.push(mark(chars.slice(start, end).join(''), target));
        cursor = end;
      }
      pieces.push(chars.slice(cursor).join(''));
      return <p key={key}>{pieces}</p>;
    });
  }

  if (!mapping.layout) return null;
  return <section className="doc-form-preview" aria-label="Расположение полей в DOCX">
    <p className="small muted">Выберите место в тексте или таблице. Оформление можно проверить в оригинале и пробном DOCX.</p>
    <div className="doc-form-sheet">{draw(mapping.layout)}</div>
  </section>;
}

export default function DocumentFormReview({ form, canPreview, onClose, onApproved }: {
  form: FormReview; canPreview: boolean; onClose: () => void; onApproved: () => void;
}) {
  const [mapping, setMapping] = useState(form.mapping);
  const [approved, setApproved] = useState(!!form.approved);
  const [selected, selectTarget] = useState<string | null>(mapping.entries[0]?.target ?? null);
  const [previewToken, setPreviewToken] = useState<string>();
  const [objectId] = useState(activeProject);
  const [projectName, setProjectName] = useState('');
  const [actDate, setActDate] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [resolution, setResolution] = useState<Resolution>();
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [issues, setIssues] = useState<string[]>([]);
  const panel = useRef<HTMLDivElement>(null);
  const running = useRef<AbortController | null>(null);
  const discovery = useRef<AbortController | null>(null);
  const path = `/libraries/versions/${form.id}`;
  const docxReview = !!mapping.layout && !!mapping.fields;
  const title = formKinds[form.kind] ?? form.kind;
  const missing = missingFields(mapping);
  const fieldOptions = Object.entries(mapping.fields ?? {});
  const entry = mapping.entries.find((item) => item.target === selected);
  const result = selected ? resolution?.fields[selected] : undefined;
  const unmapped = Object.keys(mapping.targets).filter((target) =>
    !mapping.entries.some((item) => item.target === target) &&
      (mapping.target_labels?.[target] || ['slot', 'token', 'slot_span', 'span'].includes(mapping.modes?.[target] ?? '')));
  const unresolved = Object.entries(resolution?.fields ?? {}).filter(([target, field]) =>
    field.state === 'missing' && !values[target]?.trim());
  const conflicts = Object.entries(resolution?.fields ?? {}).filter(([target, field]) =>
    field.state === 'conflict' && !values[target]?.trim() && !field.sources.some((source) => source.id === selections[target]));
  const mapBody = () => ({ format: mapping.format, profile: mapping.profile, entries: mapping.entries });
  const context = () => ({
    object_id: objectId || null, act_date: actDate || null,
    values: Object.fromEntries(Object.entries(values).filter(([, value]) => value.trim())), selections,
  });

  useEffect(() => {
    panel.current?.focus();
    return () => running.current?.abort();
  }, []);
  useEffect(() => {
    if (!objectId) return;
    const controller = new AbortController();
    void api<{ id: string; name: string }[]>('/objects', { signal: controller.signal })
      .then((objects) => setProjectName(objects.find((object) => object.id === objectId)?.name ?? ''))
      .catch(() => {});
    return () => controller.abort();
  }, [objectId]);
  useEffect(() => {
    if (!docxReview || !form.mapping.entries.length) return;
    const controller = new AbortController();
    discovery.current = controller;
    const initial = { format: form.mapping.format, profile: form.mapping.profile, entries: form.mapping.entries };
    void api<Resolution>(`${path}/resolve`, { method: 'POST', signal: controller.signal,
      json: { kind: form.kind, mapping: initial, object_id: objectId || null } })
      .then((data) => { if (!controller.signal.aborted) setResolution(data); })
      .catch(() => {});
    return () => controller.abort();
  }, [docxReview, form.kind, form.mapping, objectId, path]);

  function run(label: string, fn: (signal: AbortSignal) => Promise<void>) {
    if (running.current) return;
    if (activeProject() !== objectId) { setError('Активный проект изменился. Откройте форму заново.'); return; }
    discovery.current?.abort();
    const controller = new AbortController();
    running.current = controller;
    setBusy(label); setError(''); setMessage('');
    void fn(controller.signal).catch((e: Error) => {
      if (!controller.signal.aborted) setError(e.message);
    }).finally(() => {
      if (running.current === controller) running.current = null;
      if (!controller.signal.aborted) setBusy('');
    });
  }

  function resetResult(target?: string) {
    discovery.current?.abort();
    setResolution(undefined);
    setIssues([]);
    setPreviewToken(undefined);
    if (target) {
      const { [target]: _value, ...rest } = values;
      const { [target]: _choice, ...choices } = selections;
      setValues(rest); setSelections(choices);
    }
  }

  function changeField(target: string, field: string) {
    resetResult(target);
    const restored = mapping.entries.filter((item) => item.target !== target);
    if (field && Object.prototype.hasOwnProperty.call(mapping.targets, target)) restored.push({
      field, target, anchor: mapping.targets[target], mode: mapping.modes?.[target] ?? entry?.mode ?? 'append',
      source: entry?.source,
    });
    const { [target]: _origin, ...origins } = mapping.origins ?? {};
    setMapping({ ...mapping, entries: restored, origins });
  }

  function changeSource(source: FieldSource) {
    if (!entry) return;
    resetResult(entry.target);
    setMapping({ ...mapping, entries: mapping.entries.map((item) => item.target === entry.target ? { ...item, source } : item) });
  }

  function replaceValue(target: string, value: string) {
    discovery.current?.abort();
    setValues({ ...values, [target]: value });
    if (value.trim()) {
      const { [target]: _choice, ...choices } = selections;
      setSelections(choices);
    }
    setPreviewToken(undefined);
  }

  function chooseCandidate(target: string, source: string) {
    setSelections({ ...selections, [target]: source });
    const { [target]: _value, ...rest } = values;
    setValues(rest);
    setPreviewToken(undefined);
  }

  async function resolveFields(signal: AbortSignal) {
    const response = await api<Resolution>(`${path}/resolve`, { method: 'POST', signal,
      json: { kind: form.kind, mapping: mapBody(), ...context(), selections: {} } });
    if (signal.aborted) return;
    setResolution(response);
    setSelections((current) => Object.fromEntries(Object.entries(current).filter(([target, sourceId]) =>
      response.fields[target]?.sources.some((source) => source.id === sourceId))));
  }

  function preview(withValues: boolean) {
    run('Формирую пробный файл…', async (signal) => {
      const body = { kind: form.kind, mapping: mapBody(), ...(withValues ? { context: context() } : {}) };
      const { blob, headers } = await fetchFile(`${path}/preview`, {
        method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (signal.aborted) return;
      const format = mapping.format.startsWith('pdf') ? 'pdf' : mapping.format;
      saveFile(blob, `Проверка_формы.${format}`);
      setPreviewToken(mapping.fields ? headers.get('X-BIS-Form-Preview') ?? undefined : '');
      setIssues(resultNotes(headers));
      setMessage('Пробное заполнение скачано.');
    });
  }

  function approve() {
    run('Подтверждаю форму…', async (signal) => {
      await api(`${path}/approve`, { method: 'POST', signal,
        json: { kind: form.kind, mapping: mapBody(), preview_token: previewToken } });
      if (signal.aborted) return;
      onApproved();
      if (docxReview) {
        setApproved(true); setMessage('Форма подтверждена.');
      } else onClose();
    });
  }

  function openEvidence(source: Candidate) {
    if (source.document_id) void openFile(`/documents/${encodeURIComponent(source.document_id)}/file`,
      source.label, source.page).catch((e: Error) => setError(e.message));
  }

  return <section className="panel doc-form-review" ref={panel} tabIndex={-1}>
    <div className="row section-list-header">
      <div><h2>Проверка формы · {title}</h2><p className="small muted">{form.name}</p></div>
      <div className="row">
        <button type="button" className="btn small" onClick={() => void download(`${path}/file`, form.name).catch((e: Error) => setError(e.message))}>Скачать оригинал</button>
        <button type="button" className="btn small" disabled={!!busy} onClick={onClose}>Закрыть</button>
      </div>
    </div>
    {busy && <p role="status">{busy}</p>}
    {error && <p className="alert error" role="alert">{error}</p>}
    {message && <p className="note" role="status">{message}</p>}
    {issues.length > 0 && <ul className="note" aria-label="Замечания к заполнению">{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}
    {approved && <p className="badge approved">Форма подтверждена</p>}
    {missing.length > 0 && <p className="alert error">Обязательные поля без соответствий — {missing.map((field) => mapping.fields?.[field] ?? field).join(', ')}.</p>}
    {mapping.example && <p className="small muted">Текст без соответствий и изображения останутся как в примере. Для удаления лишнего значения выберите поле «Очистить значение из примера».</p>}
    {docxReview && <div className="row">
      <p className="small"><strong>Источники</strong> · {objectId ? `Активный проект${projectName ? ` — ${projectName}` : ''}` : 'Текущая организация, проект не выбран'}</p>
      <label className="field doc-form-date"><span>Дата документа</span><input type="date" value={actDate} disabled={!!busy} onChange={(event) => {
        resetResult(); setActDate(event.target.value); setSelections({});
      }} /></label>
      <button type="button" className="btn" disabled={!!busy || !mapping.entries.length}
        onClick={() => run('Ищу данные…', resolveFields)}>Найти данные</button>
      {objectId && <button type="button" className="btn" disabled={!!busy || !mapping.entries.length}
        onClick={() => run('Извлекаю сведения из документов проекта…', async (signal) => {
          const progress = await api<{ processed: number; remaining: number }>('/brain/facts/refresh', {
            method: 'POST', signal, timeoutMs: 120_000, json: { object_id: objectId, limit: 20 },
          });
          if (signal.aborted) return;
          setMessage(`Обработано документов — ${progress.processed}. Осталось — ${progress.remaining}.`);
          await resolveFields(signal);
        })}>Извлечь сведения из документов проекта</button>}
    </div>}
    {resolution && (unresolved.length > 0 || conflicts.length > 0) && <p className="note">
      {unresolved.length > 0 && `Нет данных для полей — ${unresolved.map(([, field]) => mapping.fields?.[field.field] ?? field.field).join(', ')}. `}
      {conflicts.length > 0 && `Нужно указать значение — ${conflicts.map(([, field]) => mapping.fields?.[field.field] ?? field.field).join(', ')}.`}
    </p>}
    {mapping.fields && <div className={mapping.layout ? 'doc-form-grid' : undefined}>
      <DocumentLayout mapping={mapping} selected={selected} onSelect={selectTarget} />
      <div className="doc-form-controls">
        <label className="field">
          <span>Место заполнения</span>
          <select value={selected ?? ''} onChange={(event) => selectTarget(event.target.value || null)}>
            <option value="">Выберите место в документе</option>
            {Object.keys(mapping.targets).map((target) => {
              const name = mapping.fields?.[mapping.entries.find((item) => item.target === target)?.field ?? ''];
              return <option key={target} value={target}>{name && `${name} · `}{sourcePosition(mapping, target)}</option>;
            })}
          </select>
        </label>
        {selected !== null && <div className="doc-form-inspector" aria-label="Настройка выбранного места">
          <p className="small muted">{sourcePosition(mapping, selected)}</p>
          <label className="field"><span>Поле документа</span>
            <select value={entry?.field ?? ''} disabled={approved || !!busy} onChange={(event) => changeField(selected, event.target.value)}>
              <option value="">Без соответствия</option>
              {fieldOptions.map(([field, label]) => <option key={field} value={field}>{label}</option>)}
            </select>
          </label>
          {entry && <p className="small muted">{mapping.origins?.[entry.target] === 'model'
            ? 'Уверенность ниже · предложение ИИ, требуется проверка'
            : mapping.origins?.[entry.target] === 'rule' ? 'Высокая уверенность · подпись или метка распознана по правилу'
            : mapping.origins?.[entry.target] === 'learned' ? 'Из ранее утверждённой карты · проверьте соответствие'
            : approved ? 'Сохранённое соответствие' : 'Указано вручную'}</p>}
          {entry && docxReview && <label className="field"><span>Предпочтительный источник</span>
            <select value={entry.source ?? 'auto'} onChange={(event) => changeSource(event.target.value as FieldSource)} disabled={approved || !!busy}>
              {Object.entries(sourceLabels).map(([source, label]) => <option key={source} value={source}>{label}</option>)}
            </select>
          </label>}
          {entry && docxReview && (entry.source === 'manual' || result?.state === 'missing' ||
            (result?.state === 'conflict' && !result.sources.length) || values[entry.target] !== undefined) && <label className="field">
            <span>Значение для текущего документа</span>
            <textarea rows={2} value={values[entry.target] ?? ''} disabled={!!busy} onChange={(event) => replaceValue(entry.target, event.target.value)} />
          </label>}
          {result?.state === 'resolved' && <>
            <p className="doc-form-value"><strong>Найденное значение</strong><br />{result.value}</p>
            {result.sources.map((source) => <SourceEvidence key={source.id} source={source} onOpen={openEvidence} />)}
          </>}
          {result?.state === 'missing' && !values[selected]?.trim() && <p className="small muted">Подтверждённых данных нет.</p>}
          {result?.issues?.length && <p className="small muted">Проверьте данные для ролей — {result.issues.join(', ')}.</p>}
          {result?.state === 'conflict' && result.sources.length > 0 && <fieldset className="doc-form-choices" disabled={!!busy}>
            <legend>Какое значение использовать?</legend>
            {result.sources.map((source) => <div key={source.id}>
              <label><input type="radio" name={`source-${selected}`} checked={selections[selected] === source.id}
                onChange={() => chooseCandidate(selected, source.id)} /> {source.value}</label>
              <SourceEvidence source={source} onOpen={openEvidence} />
            </div>)}
          </fieldset>}
        </div>}
        {mapping.entries.length > 0 && <ul className="doc-form-field-list" aria-label="Сопоставленные поля">
          {mapping.entries.map((item) => <li key={item.target}>
            <button type="button" className="btn small" aria-pressed={selected === item.target} onClick={() => selectTarget(item.target)}
              title={sourcePosition(mapping, item.target)}>{mapping.fields?.[item.field] ?? item.field}</button>
            {mapping.origins?.[item.target] === 'model' && <span className="small muted">Проверить предложение ИИ</span>}
            {unresolved.some(([target]) => target === item.target) && <span className="small muted">Нет данных</span>}
            {conflicts.some(([target]) => target === item.target) && <span className="small text-bad">Конфликт источников</span>}
          </li>)}
        </ul>}
        {!approved && unmapped.length > 0 && <details className="doc-form-unmapped"><summary>Места без соответствия · {unmapped.length}</summary>
          <ul className="doc-form-field-list">{unmapped.map((target) => <li key={target}>
            <button type="button" className="btn small" onClick={() => selectTarget(target)}>{sourcePosition(mapping, target)}</button>
          </li>)}</ul>
        </details>}
      </div>
    </div>}
    <div className="row doc-form-actions">
      {canPreview && <button type="button" className="btn" disabled={!!busy || missing.length > 0} onClick={() => preview(false)}>Скачать пробное заполнение</button>}
      {canPreview && docxReview && <button type="button" className="btn" disabled={!!busy || missing.length > 0 || conflicts.length > 0}
        onClick={() => preview(true)}>Пробник с найденными данными</button>}
      {!approved && <button type="button" className="btn primary" disabled={!!busy || previewToken === undefined}
        onClick={approve}>Проверено, использовать форму</button>}
      {approved && docxReview && <button type="button" className="btn primary" disabled={!!busy || conflicts.length > 0}
        onClick={() => run('Создаю документ…', async (signal) => {
          const { blob, filename, headers } = await fetchFile(`${path}/generate`, {
            method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(context()),
          });
          if (signal.aborted) return;
          saveFile(blob, filename ?? `${title}.docx`);
          setIssues(resultNotes(headers)); setMessage('Черновик создан и скачан.');
        })}>Сформировать DOCX</button>}
    </div>
  </section>;
}

import { useEffect, useRef, useState } from 'react';
import { api, download, openFile, STATUS_LABELS, type DocumentRow } from '../../api';
import { PageHead } from '../../components';
import { navigate } from '../navigation';
import TaskComposer from './TaskComposer';
import { askAssistant } from './assistantTask';

type Category = 'documents' | 'schemes' | 'project' | 'contracts';
interface Item extends DocumentRow { reading_status: string; chunks: number; contract_kind: string; reading: { warnings?: string[]; empty_pages?: number[]; segments?: number; with_text?: number } }
interface Result { documents: Item[]; total: number; has_more: boolean; needs_project: boolean }
const TITLES = { documents: 'Исполнительная документация', schemes: 'Исполнительные схемы', project: 'Проект', contracts: 'Договоры' };
const PLACEHOLDERS = {
  documents: 'Например: подготовь АООК на фундаментную плиту по имеющимся актам и протоколам…',
  schemes: 'Например: подготовь исполнительную схему участка 123 по проекту и загруженной съёмке…',
  project: 'Например: какие кабели и объёмы предусмотрены проектом на участке 123?',
  contracts: 'Например: какие сроки, аванс и гарантийные обязательства установлены генподрядом?',
};
const READ: Record<string, string> = { indexed: 'Текст доступен', partial: 'Прочитано частично', unread: 'Текст не прочитан', reading: 'Перечитывается…' };

export default function WorkspaceDocuments({ category }: { category: Category }) {
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const request = useRef<AbortController | null>(null);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [selected, setSelected] = useState<Item | null>(null);
  const [check, setCheck] = useState<{ acts: number; complete: number; missing_refs: number } | null>(null);
  const [needs, setNeeds] = useState<{ document_id: string; title: string; works: string; number: string | null; reason: string }[]>([]);
  async function load(more = false) {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setError('');
    try {
      const result = await api<Result>(`/workspace/documents?category=${category}&q=${encodeURIComponent(appliedQuery)}&offset=${more ? data?.documents.length ?? 0 : 0}`, { signal: controller.signal });
      setData((old) => more && old ? { ...result, documents: [...old.documents, ...result.documents] } : result);
      if (category === 'schemes' && !more) {
        const requested = await api<{ items: typeof needs }>('/workspace/scheme-needs', { signal: controller.signal });
        setNeeds(requested.items);
      }
    } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); } finally { if (request.current === controller) setBusy(false); }
  }
  useEffect(() => { void load(); return () => request.current?.abort(); }, [category, appliedQuery]);
  useEffect(() => { if (selected) {
    const input = document.querySelector<HTMLTextAreaElement>('.task-composer textarea');
    input?.focus(); input?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  } }, [selected]);
  const rows = data?.documents.filter((d) => filter === 'all' || (category === 'contracts' ? d.contract_kind === filter :
    filter === 'ready' ? d.status === 'approved' : d.status !== 'approved')) ?? [];
  async function checkPackage() {
    setChecking(true); setError('');
    try { setCheck(await api('/archive/id-check')); } catch (e) { setError((e as Error).message); } finally { setChecking(false); }
  }
  function searchNow() { if (busy) return; if (query === appliedQuery) void load(); else setAppliedQuery(query); }
  return <>
    <PageHead title={TITLES[category]} hint={category === 'project' ? 'Рабочие чертежи, спецификации и состояние их чтения. Вопрос можно задать по любому документу.' :
      category === 'contracts' ? 'Генподряд и субподряд выбранного проекта. Условия и ответы — с привязкой к оригиналам.' :
      'Перечень документов, состояние готовности и задания на подготовку — без ручного заполнения реквизитов.'}>
      <button className="btn" disabled={busy} onClick={() => void load()}>Обновить список</button>
      <button className="btn" onClick={() => navigate('id', 'libraries')}>Подключить документы</button>
    </PageHead>
    {error && <div role="alert" className="alert error">{error}</div>}
    {data?.needs_project && <div className="note">Выберите объект вверху страницы, чтобы видеть его документы и задавать вопросы по проекту.</div>}
    <TaskComposer placeholder={PLACEHOLDERS[category]} documentId={selected?.id} title={selected?.title} />
    {selected && <button className="btn small" onClick={() => setSelected(null)}>Снять выбор документа</button>}
    {category === 'documents' && <div className="panel compact-panel"><div className="row"><strong>Готовность комплекта ИД</strong>
      <button className="btn small" disabled={checking} onClick={() => void checkPackage()}>{checking ? 'Проверяю…' : 'Проверить комплектность'}</button></div>
      {check && <p>Актов: {check.acts} · комплектных: {check.complete} · отсутствующих ссылок: {check.missing_refs}</p>}</div>}
    {category === 'schemes' && <div className="panel"><h2>Какие схемы ещё нужны</h2><p className="small muted">По ссылкам и замечаниям имеющихся актов. Задание на другую схему можно дать текстом или голосом.</p>
      {!needs.length && <p className="muted">Не найдены акты с отсутствующими схемами.</p>}
      {needs.map((item, index) => <div className="database-connection" key={`${item.document_id}:${index}`}><strong>{item.number ? `Схема № ${item.number}` : item.works || item.title}</strong><p className="small muted">{item.title} · {item.reason}</p>
        <button className="btn small primary" onClick={() => askAssistant(`Подготовь исполнительную схему${item.number ? ` № ${item.number}` : ''} по работам акта «${item.title}». Используй проект и имеющуюся съёмку; покажи, если фактических данных недостаточно.`, item.document_id, item.title)}>Поручить подготовку схемы</button></div>)}
    </div>}
    <div className="panel">
      <div className="row workspace-search"><input aria-label="Поиск документов" placeholder="Имя файла, номер документа или фрагмент текста" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') searchNow(); }} />
        <button className="btn" disabled={busy} onClick={searchNow}>Найти в списке</button>{appliedQuery && <button className="btn small" onClick={() => { setQuery(''); setAppliedQuery(''); }}>Сбросить поиск</button>}</div>
      <div className="row section-list-header"><h2>Документы {data ? `· ${data.total}` : ''}</h2>
        <div className="tabs">{(category === 'contracts' ? [['all','Все'], ['general','Генподряд'], ['subcontract','Субподряд'], ['unclassified','Вид не определён']] :
          category === 'documents' || category === 'schemes' ? [['all','Все'], ['ready','Готово'], ['pending','На подготовке']] : [['all','Все']]).map(([key,label]) =>
          <button key={key} className={`tab ${filter === key ? 'active' : ''}`} onClick={() => setFilter(key)}>{label}</button>)}</div>
      </div>
      {busy && !data && <p className="muted">Загружаю документы…</p>}
      {data && !rows.length && <p className="muted">Документов в этом списке пока нет. Подключите папку в «Базах данных» или дайте задание помощнику.</p>}
      <div className="workspace-documents">{rows.map((doc) => <article key={doc.id} className={`workspace-document ${selected?.id === doc.id ? 'selected' : ''}`}>
        <div className="workspace-document-copy"><strong>{doc.title}</strong><p className="small muted">{doc.file_name || doc.number || 'Документ BIS'}</p>
          <div className="row"><span className={`badge ${doc.status}`}>{STATUS_LABELS[doc.status] ?? doc.status}</span>
            <span className={`small ${doc.reading_status === 'indexed' ? 'muted' : 'text-bad'}`}>{READ[doc.reading_status]}{doc.chunks ? ` · ${doc.chunks} фрагм.` : ''}</span></div>
          {!!doc.reading.warnings?.length && <p className="small text-bad">{doc.reading.warnings.join('; ')}</p>}
          {!!doc.reading.empty_pages?.length && <p className="small text-bad">Нет текста на страницах: {doc.reading.empty_pages.join(', ')}</p>}
        </div>
        <div className="row document-actions"><button className="btn small primary" onClick={() => setSelected(doc)}>Задать вопрос</button>
          {doc.file_name && <button className="btn small" onClick={() => void openFile(`/documents/${doc.id}/file`, (doc.file_name || doc.title)).catch((e) => setError(e.message))}>Открыть</button>}
          {category === 'schemes' && doc.file_name && <button className="btn small" onClick={() => void download(`/documents/${doc.id}/file`, doc.file_name ?? undefined).catch((e) => setError(e.message))}>Скачать</button>}
        </div>
      </article>)}</div>
      {data?.has_more && <button className="btn" disabled={busy} onClick={() => void load(true)}>Показать ещё</button>}
    </div>
  </>;
}

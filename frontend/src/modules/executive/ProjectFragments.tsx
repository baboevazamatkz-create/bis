import { useEffect, useRef, useState } from 'react';
import { activeProject, api, download, openFile } from '../../api';

interface Fragment {
  chunk_id: string;
  title: string;
  file_name: string | null;
  content: string;
  page: number | null;
  sheet_name: string | null;
  reading_status: string;
  original_url: string | null;
}

interface Coverage { files: number; ready: number; partial: number; error: number; unknown: number }

export default function ProjectFragments() {
  const project = activeProject();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{ query: string; results: Fragment[] } | null>(null);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [coverageError, setCoverageError] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!project) return;
    const controller = new AbortController();
    void api<Coverage>('/libraries/project/coverage', { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setCoverage(data); })
      .catch(() => { if (!controller.signal.aborted) setCoverageError(true); });
    return () => { controller.abort(); request.current?.abort(); };
  }, [project]);

  async function search() {
    const text = query.trim();
    if (!text || !project) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setResults(null);
    setError('');
    try {
      const params = new URLSearchParams({ scope: 'project', q: text, fragments: 'true' });
      const result = await api<{ query: string; results: Fragment[] }>(`/knowledge/search?${params}`, { signal: controller.signal });
      if (!controller.signal.aborted) setResults(result);
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function openOriginal(hit: Fragment) {
    if (!hit.original_url) return;
    setError('');
    const path = hit.original_url.replace(/^\/api\//, '/');
    const name = hit.file_name || hit.title;
    try {
      if (/\.xls[xm]$/i.test(name)) await download(path, name);
      else await openFile(path, name, hit.page);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (!project) return <p className="note">Выберите объект вверху страницы, чтобы искать фрагменты.</p>;
  return <>
    <form className="row workspace-search" onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <input aria-label="Поиск фрагментов" placeholder="Обозначение, марка или текст документа" value={query} onChange={(event) => setQuery(event.target.value)} />
      <button className="btn primary" disabled={busy || !query.trim()}>Найти фрагменты</button>
    </form>
    <p className="small muted">
      {coverage ? `Охват извлечения текста в подключённых файлах · всего ${coverage.files}, с полным охватом ${coverage.ready}, частично ${coverage.partial}, ошибок ${coverage.error}, охват неизвестен ${coverage.unknown}. `
        : coverageError ? 'Не удалось получить охват чтения. ' : 'Загружаю охват чтения… '}
      <a href="#/id/libraries">Файлы и ошибки чтения базы проекта</a>
    </p>
    {busy && <p role="status">Ищу фрагменты…</p>}
    {error && <div className="alert error" role="alert">{error}</div>}
    {results && !results.results.length && <p className="note" role="status">Совпадений в извлечённом тексте по запросу «{results.query}» нет.</p>}
    <div className="workspace-documents">
      {results?.results.map((hit) => <article className="workspace-document" key={hit.chunk_id}>
        <div className="workspace-document-copy">
          <strong>{hit.file_name || hit.title}</strong>
          <p className="small muted">
            {hit.page ? `Физическая страница ${hit.page}` : hit.sheet_name ? `Лист «${hit.sheet_name}»` : 'Фрагмент документа'}
            {hit.reading_status === 'partial' && <span className="text-bad"> · Неполное чтение</span>}
            {hit.reading_status === 'unknown' && <span> · Охват чтения неизвестен</span>}
          </p>
          <blockquote className="project-fragment">{hit.content}</blockquote>
        </div>
        {hit.original_url && <div className="row document-actions">
          <button type="button" className="btn small" onClick={() => void openOriginal(hit)}>
            {hit.file_name && /\.xls[xm]$/i.test(hit.file_name) ? 'Скачать оригинал' : 'Открыть оригинал'}
          </button>
        </div>}
      </article>)}
    </div>
  </>;
}

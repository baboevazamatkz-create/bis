import { useEffect, useRef, useState } from 'react';
import { api, download } from '../../api';

interface Run { id: string; text: string; status: string; revision: number; error?: string; gaps?: { field: string; message: string }[]; documents?: { id: string; title: string }[] }
const STATUS: Record<string, string> = { queued: 'В очереди', running: 'Готовится', needs_data: 'Нужны данные', needs_input: 'Нужны данные', blocked: 'Нужны данные', ready_for_review: 'Готов к проверке', error: 'Ошибка обработки' };

export default function RunList({ canWrite }: { canWrite: boolean }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const loading = useRef(false);
  async function load(signal?: AbortSignal) {
    if (loading.current) return;
    loading.current = true;
    try { const rows = await api<Run[]>('/executive/runs', { signal }); if (!signal?.aborted) setRuns(rows); }
    finally { loading.current = false; }
  }
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (loading.current) { timer = setTimeout(poll, 100); return; }
      try { await load(controller.signal); } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 10000);
    };
    void poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  async function retry(run: Run) {
    if (busy) return;
    setBusy(run.id); setError(''); setNotice('');
    try {
      const changed = await api<Run>(`/executive/runs/${run.id}/resume`, { method: 'POST', json: { revision: run.revision } });
      setRuns((old) => old.map((r) => r.id === changed.id ? changed : r));
      setNotice('Задание поставлено на повторную проверку по имеющимся источникам.');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  }
  return <section className="panel"><h2>Задания на подготовку ИД</h2>
    {error && <p className="alert error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!runs.length && <p className="muted">Поручите подготовку помощнику. Здесь появятся результаты и недостающие данные.</p>}
    {runs.map((run) => <details className="database-connection" key={run.id}>
      <summary>{STATUS[run.status] ?? run.status} · {run.text}</summary>
      {run.error && <p className="text-bad">{run.error}</p>}
      {run.gaps?.map((gap, index) => <p key={`${gap.field}:${index}`} className="small">{gap.message}</p>)}
      {!!run.documents?.length && <p className="small">Документов: {run.documents.length}. Черновики доступны в реестре ниже.</p>}
      <div className="row">
        {canWrite && <button className="btn small" disabled={!!busy || ['queued', 'running'].includes(run.status)} onClick={() => void retry(run)}>Повторить проверку задания</button>}
        {!!run.documents?.length && <button className="btn small" disabled={['queued', 'running'].includes(run.status)} onClick={() => void download(`/executive/runs/${run.id}/bundle`, 'Комплект_ИД.zip').catch((e) => setError(e.message))}>Скачать комплект</button>}
      </div>
    </details>)}
  </section>;
}

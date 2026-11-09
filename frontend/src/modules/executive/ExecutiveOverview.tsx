import { useEffect, useState } from "react";
import { activeProject, setActiveProject, api, download, openFile, STATUS_LABELS, type DocumentRow } from "../../api";
import { PageHead, Stat, useObjects } from "../../components";
import Icon from "../../components/Icon";
import { navigate } from "../navigation";

interface Check {
  acts: number; complete: number; missing_refs: number; with_issues: number;
  items: { document_id: string; title: string; complete: boolean; issues: string[];
    refs: { title: string; number: string; found: boolean }[] }[];
}

export default function ExecutiveOverview({ checkOnly = false }: { checkOnly?: boolean }) {
  const objects = useObjects();
  const [objectId, setObjectId] = useState(activeProject());
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [check, setCheck] = useState<Check | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  useEffect(() => { if (objects.length === 1) setObjectId(objects[0].id); }, [objects]);
  useEffect(() => {
    let active = true;
    setCheck(null);
    setLoading(true);
    setError("");
    api<DocumentRow[]>(`/documents?workspace=executive&limit=500${objectId ? `&object_id=${objectId}` : ""}`)
      .then((docs) => { if (active) setDocuments(docs); })
      .catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [objectId]);

  async function verify() {
    setBusy(true); setError("");
    try { setCheck(await api<Check>(`/archive/id-check${objectId ? `?object_id=${objectId}` : ""}`)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function exportRegistry() {
    if (!objectId) return;
    setBusy(true); setError("");
    try {
      const doc = await api<DocumentRow>(`/objects/${objectId}/id-registry/export`, { method: "POST" });
      await download(`/documents/${doc.id}/file`, doc.file_name ?? undefined);
      setDocuments((rows) => [doc, ...rows]);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const drafts = documents.filter((d) => d.status === "draft" || d.status === "rejected");
  const issues = documents.filter((d) => d.issues?.length);
  return <>
    <PageHead title={checkOnly ? "Комплектность документации" : "Обзор проекта"}
      hint="Рабочее пространство инженера ПТО: исходные документы, подготовка актов, исполнительные схемы и проверка комплектов.">
      <button className="btn" disabled={!objectId || busy} title={!objectId ? "Выберите объект для выгрузки реестра" : "Выгрузить реестр в Excel"} onClick={() => void exportRegistry()}>Выгрузить реестр ИД</button>
    </PageHead>
    {!activeProject() && <div className="pto-toolbar">
      <div className="field"><label htmlFor="pto-object">Объект строительства</label>
        <select id="pto-object" value={objectId} onChange={(e) => { setObjectId(e.target.value); setActiveProject(e.target.value); }}>
          <option value="">Выберите объект</option>{objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </div>
    </div>}
    {error && <div role="alert" className="alert error">{error}</div>}
    {!checkOnly && <>
      <div className="pto-hero panel">
        <div className="hero-copy"><span className="pto-eyebrow"><span className="hero-status-dot" /> СИНАПС · ИНЖЕНЕР ПТО</span><h2>От выполненных работ<br />к готовому комплекту.</h2>
          <p>Опишите, что сделано на объекте.<br />Основания, даты и приложения соберёт Синапс.</p><div className="hero-buttons">
          <button className="btn primary" onClick={() => navigate("id", "pipeline")}>Сообщить о работе <Icon name="arrow" size={17} /></button>
          <button className="btn hero-secondary" onClick={() => navigate("id", "archive")}>Загрузить документы</button></div></div>
        <div className="hero-documents" aria-hidden="true"><div className="hero-sheet sheet-back" /><div className="hero-sheet sheet-mid" /><div className="hero-sheet sheet-front"><div className="sheet-top"><Icon name="documents" size={21} /><span>ИСПОЛНИТЕЛЬНАЯ<br />ДОКУМЕНТАЦИЯ</span></div><div className="sheet-title">Каждая деталь<br />на своём месте.</div><div className="sheet-lines"><i /><i /><i /></div><div className="sheet-bottom">РД <span>→</span> АКТ <span>→</span> КОМПЛЕКТ</div></div></div>
      </div>
      <div className="stats">
        <Stat label="Документов ИД" value={loading ? "…" : `${documents.length}${documents.length === 500 ? "+" : ""}`} />
        <Stat label="На проверке" value={loading ? "…" : drafts.length} tone={drafts.length ? "warn" : undefined} />
        <Stat label="С замечаниями" value={loading ? "…" : issues.length} tone={issues.length ? "bad" : undefined} />
      </div>
      <div className="pto-actions">
        {[["pipeline", "01", "Сообщить о работе", "Синапс соберёт черновик ИД по РД, журналу и документам качества"],
          ["chat", "02", "Поручить помощнику", "Подготовить ИД по имеющимся данным и документам"],
          ["schemes", "03", "Исполнительная схема", "Подготовить схему по съёмке и замерам"]].map(([page, n, title, text]) =>
          <button key={page} className="pto-action" onClick={() => navigate("id", page)}><span className="action-card-top"><Icon name={page} size={22} /><span>{n}</span></span><strong>{title}<Icon name="arrow" size={17} /></strong><small>{text}</small></button>)}
      </div>
    </>}
    <div className="panel">
      <div className="pto-section-head"><div><h2>Проверка комплекта ИД</h2><p className="muted">АОСР и АООК → документы качества, протоколы, исполнительные схемы и РД.</p></div>
        <button className="btn primary" disabled={busy} onClick={() => void verify()}>{busy ? "Проверяю…" : "Проверить комплектность"}</button>
      </div>
      {!check && <p className="muted">Запустите проверку: система покажет, какие документы найдены и чего не хватает по ссылкам из актов.</p>}
      {check && <>
        <div className="stats"><Stat label="Проверено актов" value={check.acts} /><Stat label="Комплектных" value={check.complete} tone="ok" />
          <Stat label="Не найдены документы по ссылкам" value={check.missing_refs} tone={check.missing_refs ? "bad" : undefined} /></div>
        {check.acts === 0 && <p className="muted">В архиве пока нет актов для проверки. Загрузите существующие акты или подготовьте первый.</p>}
        {check.acts > 0 && check.items.every((a) => a.complete) && <div className="alert ok">По проверенным ссылкам комплект полон.</div>}
        {check.items.filter((a) => !a.complete).map((act) => <div className="note warning" key={act.document_id}>
          <button className="link" onClick={() => void openFile(`/documents/${act.document_id}/file`, act.title)}>{act.title}</button>
          <ul>{act.issues.map((issue, i) => <li key={i}>{issue}</li>)}
            {act.refs.filter((r) => !r.found).map((r, i) => <li key={`ref-${i}`}>Не найден: {r.title} № {r.number}</li>)}</ul>
        </div>)}
      </>}
    </div>
    {!checkOnly && <div className="panel"><div className="pto-section-head"><h2>Последние документы ИД</h2><button className="btn small" onClick={() => navigate("id", "documents")}>Открыть реестр</button></div>
      {loading ? <p className="muted">Загрузка…</p> : documents.length === 0 ? <p className="muted">Начните с загрузки РД, сертификатов и протоколов или создайте первый акт.</p> : <table><thead><tr><th>Документ</th><th>Статус</th><th /></tr></thead><tbody>
        {documents.slice(0, 6).map((d) => <tr key={d.id}><td>{d.title}</td><td><span className={`badge ${d.status}`}>{STATUS_LABELS[d.status] ?? d.status}</span></td><td><button className="btn small" onClick={() => void openFile(`/documents/${d.id}/file`, d.title)}>Открыть</button></td></tr>)}
      </tbody></table>}
    </div>}
  </>;
}

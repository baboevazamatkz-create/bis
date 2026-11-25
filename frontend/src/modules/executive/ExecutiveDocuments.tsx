import { useEffect, useState, type ReactNode } from "react";
import { activeProject, api, openFile, STATUS_LABELS } from "../../api";
import { PageHead } from "../../components";
import { navigate } from "../navigation";
import { askAssistant } from "./assistantTask";
import WorkspaceDocuments from "./WorkspaceDocuments";

interface Work {
  name: string;
  source: { document_id: string; page: number | null } | null;
  act_ids: string[];
}
interface Section {
  label: string;
  works: Work[];
}
interface Area extends Section {
  subobjects: Section[];
}
interface Act {
  id: string;
  title: string;
  status: string;
  file_name: string | null;
}
interface Structure {
  needs_project: boolean;
  indexed_documents: number;
  areas: Area[];
  acts: Act[];
  unassigned_act_ids: string[];
}

const views = [
  ["documents", "По участкам"],
  ["documents/registry", "Общий реестр"],
  ["documents/schemes", "Исполнительные схемы"],
];

export default function ExecutiveDocuments({ page, canWrite }: { page: string; canWrite: boolean }) {
  const navigation = <nav className="tabs id-view-tabs" aria-label="Исполнительная документация">
      {views.map(([route, name]) => <a key={route} className={`tab ${route === page ? "active" : ""}`}
        aria-current={route === page ? "page" : undefined} href={`#/id/${route}`}>{name}</a>)}
    </nav>;
  return page === "documents" ? <ProjectWorkIndex navigation={navigation} /> :
    <WorkspaceDocuments key={page} category={page === "documents/schemes" ? "schemes" : "documents"} canWrite={canWrite} navigation={navigation} />;
}

function ProjectWorkIndex({ navigation }: { navigation: ReactNode }) {
  const [data, setData] = useState<Structure | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [areaLabel, setAreaLabel] = useState("");
  const [work, setWork] = useState<Work | null>(null);
  const [unassigned, setUnassigned] = useState(false);
  const project = activeProject();

  useEffect(() => {
    if (!project) return;
    const controller = new AbortController();
    api<Structure>("/workspace/id-structure", { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setData(result);
      })
      .catch((reason: Error) => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, [project, revision]);

  function refresh() {
    setError("");
    setData(null);
    setAreaLabel("");
    setWork(null);
    setUnassigned(false);
    setRevision((value) => value + 1);
  }

  function open(path: string, title: string, page?: number | null) {
    void openFile(path, title, page).catch((reason: Error) => setError(reason.message));
  }

  const area = data?.areas.find((item) => item.label === areaLabel) ?? data?.areas[0];
  const source = work?.source;
  const ids = new Set(unassigned ? data?.unassigned_act_ids : work?.act_ids);
  const acts = data?.acts.filter((act) => ids.has(act.id)) ?? [];

  function renderSection(section: Section, heading: string) {
    return <section key={section.label} className="id-work-section">
      <h3>{heading}</h3>
      {section.works.length ? <div className="id-work-choices">
        {section.works.map((item) => <button className="btn" key={item.name}
          aria-pressed={work === item && !unassigned} onClick={() => { setWork(item); setUnassigned(false); }}>
          <span>{item.name}</span><span className="muted small">{item.act_ids.length} акт.</span>
        </button>)}
      </div> : <p className="muted">В прочитанных файлах работы здесь пока не определены.</p>}
    </section>;
  }

  return <>
    <PageHead title="Исполнительная документация"
      description="Участки, подобъекты, работы и акты выбранного проекта.">
      <button className="btn" disabled={!project || (!data && !error)} onClick={refresh}>Обновить структуру</button>
    </PageHead>
    {navigation}
    {error && <p className="alert error" role="alert">{error}</p>}
    {(!project || data?.needs_project) && <p className="note">Выберите проект в верхней панели.</p>}
    {project && !data && !error && <p className="muted" role="status">Загружаю структуру проекта…</p>}
    {data && !data.needs_project && <>
      {!data.indexed_documents && <div className="note">
        <p>Текст проектных файлов пока недоступен. Подключите папку или дождитесь окончания чтения.</p>
        <button className="btn small" onClick={() => navigate("id", "libraries")}>Базы данных</button>
      </div>}
      <div className="id-work-layout">
        <section className="panel id-work-panel">
          <h2>Участки и подобъекты</h2>
          <div className="id-work-choices">
            {data.areas.map((item) => <button className="btn" key={item.label} aria-pressed={item === area && !unassigned}
              onClick={() => { setAreaLabel(item.label); setWork(null); setUnassigned(false); }}>{item.label}</button>)}
            {!!data.unassigned_act_ids.length && <button className="btn" aria-pressed={unassigned} onClick={() => { setWork(null); setUnassigned(true); }}>
              <span>Акты без привязки</span><span className="muted small">{data.unassigned_act_ids.length}</span>
            </button>}
          </div>
          {!data.areas.length && <p className="muted">В доступном тексте участки пока не определены.</p>}
        </section>
        <section className="panel id-work-panel" aria-label="Работы">
          <h2>{!unassigned && area ? area.label : "Работы"}</h2>
          {!unassigned && area ? <>
            {(area.works.length > 0 || !area.subobjects.length) && renderSection(area, "Работы")}
            {area?.subobjects.map((item) => renderSection(item, item.label))}
          </> : <p className="muted">{unassigned ? "Место выполнения для этих актов не определилось." : "Выберите участок, чтобы открыть его работы."}</p>}
        </section>
        <section className="panel id-work-panel id-work-result" aria-label="Акты">
          <h2>{unassigned ? "Акты без привязки" : "Акты по работе"}</h2>
          {work ? <section className="id-work-section">
            <h3>{work.name}</h3>
            {source && <button className="btn small" onClick={() => open(`/documents/${source.document_id}/file`, work.name, source.page)}>
              {source.page ? `Открыть основание · стр. ${source.page}` : "Открыть основание"}</button>}
            {!acts.length && <p className="muted">По этой работе пока нет привязанных актов.</p>}
          </section> : !unassigned && <p className="muted">Выберите работу, чтобы открыть её акты.</p>}
          {!!acts.length && <div className="workspace-documents">{acts.map((act) => <article className="workspace-document" key={act.id}>
            <div className="workspace-document-copy">
              <strong>{act.title}</strong>
              <div><span className={`badge ${act.status}`}>{STATUS_LABELS[act.status] ?? act.status}</span></div>
            </div>
            <div className="row document-actions">
              {act.file_name && <button className="btn small" onClick={() => open(`/documents/${act.id}/file`, act.file_name ?? act.title)}>Открыть акт</button>}
              <button className="btn small primary" onClick={() => askAssistant(`Проверь комплектность акта «${act.title}», включая исполнительные схемы.`, act.id, act.title)}>Спросить по акту</button>
            </div>
          </article>)}</div>}
        </section>
      </div>
    </>}
  </>;
}

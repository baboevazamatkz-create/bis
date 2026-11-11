import { Suspense, useEffect, useRef, useState } from "react";
import { api, DEMO, setToken, setOrganizationScope, activeProject, setActiveProject, type User } from "./api";
import { previewFor } from "./demo/mock";
import Icon from "./components/Icon";
import ExecutiveWorkspace from "./modules/executive/ExecutiveWorkspace";
import { ID_NAV, parseRoute } from "./modules/navigation";

export default function App() {
  const [mobile, setMobile] = useState(() => matchMedia("(max-width: 960px)").matches);
  const [menuOpen, setMenuOpen] = useState(false);
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem("bis:theme") ?? "light"; } catch { return "light"; } });
  const nav = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  const [projectError, setProjectError] = useState("");
  const [startError, setStartError] = useState("");
  const [user, setUser] = useState<User | null>(null);
  const [project, setProject] = useState("");
  const [orgName, setOrgName] = useState("");
  const [objects, setObjects] = useState<{ id: string; name: string; code: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const [preview, setPreview] = useState<{ type: string; content: string; title: string } | null | undefined>(undefined);
  async function loadObjects() {
    setProjectError('');
    try {
      const rows = await api<typeof objects>('/objects'); setObjects(rows);
      if (activeProject() && !rows.some((r) => r.id === activeProject())) setActiveProject('');
      if (rows.length === 1 && !activeProject()) setActiveProject(rows[0].id);
    } catch (e) { setProjectError((e as Error).message); }
  }
  useEffect(() => {
    const onLogout = () => setUser(null);
    const onRoute = () => setRoute(parseRoute(window.location.hash));
    const onPreview = (e: Event) => setPreview(previewFor((e as CustomEvent<string>).detail));
    window.addEventListener("bis:logout", onLogout);
    window.addEventListener("hashchange", onRoute);
    window.addEventListener("bis:preview", onPreview);
    api<{ user: User }>("/auth/me").then((r) => setUser(r.user)).catch((e) => { setToken(null); setStartError(e.message); }).finally(() => setLoading(false));
    return () => {
      window.removeEventListener("bis:logout", onLogout);
      window.removeEventListener("hashchange", onRoute);
      window.removeEventListener("bis:preview", onPreview);
    };
  }, []);
  useEffect(() => {
    if (!user) return;
    setOrganizationScope(user.organization_id);
    setPreview(undefined);
    setProject(activeProject());
    void api<{ name: string }>("/org").then((o) => setOrgName(o.name)).catch(() => {});
    void loadObjects();
    const changed = () => { setProject(activeProject()); setPreview(undefined); };
    window.addEventListener("bis:project", changed);
    return () => window.removeEventListener("bis:project", changed);
  }, [user?.organization_id]);
  useEffect(() => {
    document.title = route.workspace === "id" ? "BIS · Исполнительная документация" : "BIS · Платформа";
  }, [route.workspace]);
  useEffect(() => {
    const query = matchMedia("(max-width: 960px)");
    const change = () => { setMobile(query.matches); setMenuOpen(false); };
    query.addEventListener("change", change);
    return () => query.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("bis:theme", theme); } catch { /* optional */ }
  }, [theme]);
  useEffect(() => { setMenuOpen(false); }, [route.page, route.workspace, project]);
  useEffect(() => {
    if (!mobile || !menuOpen) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    nav.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") { setMenuOpen(false); menuButton.current?.focus(); } };
    window.addEventListener("keydown", escape);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", escape); };
  }, [mobile, menuOpen]);
  if (loading) return <div className="login muted">Загрузка…</div>;
  if (!user && DEMO) return <div className="login muted">Демо-данные не загружены</div>;
  if (!user) return <div className="login"><h1>BIS</h1><p>Не удалось открыть рабочее пространство.</p><p className="muted">{startError || "Перезапустите локальный BIS-Pilot."}</p><button className="btn primary" onClick={() => window.location.reload()}>Повторить подключение</button></div>;
  setOrganizationScope(user.organization_id);
  const groups = ID_NAV;
  return (
    <div className={`layout workspace-${route.workspace}`}>
      <a className="skip-link" href="#main-content" onClick={(e) => { e.preventDefault(); document.getElementById("main-content")?.focus(); }}>Перейти к содержимому</a>
      {mobile && menuOpen && <button className="nav-backdrop" aria-label="Закрыть навигацию" onClick={() => { setMenuOpen(false); menuButton.current?.focus(); }} />}
      <nav ref={nav} id="app-navigation" className={`sidebar ${menuOpen ? "is-open" : ""}`} hidden={mobile && !menuOpen}
        aria-label={route.workspace === "id" ? "Блок исполнительной документации" : "Другие направления BIS"}
        onKeyDown={(e) => {
          if (!mobile || !menuOpen || e.key !== "Tab") return;
          const items = Array.from(nav.current?.querySelectorAll<HTMLElement>("a[href], button, summary") ?? []).filter((el) => el.getClientRects().length > 0);
          const first = items[0], last = items[items.length - 1];
          if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
          else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
        }}>
        <div className="brand"><a href={`#/${route.workspace}/overview`} aria-label="BIS — рабочий стол"><span className="brand-mark">b<span>i</span>s</span><span className="brand-caption">Construction intelligence</span></a>
          {mobile && <button className="icon-button" aria-label="Закрыть меню" onClick={() => { setMenuOpen(false); menuButton.current?.focus(); }}><Icon name="close" /></button>}</div>
        <div className="workspace-label"><span className="workspace-dot" />{route.workspace === "id" ? "Исполнительная документация" : "Платформа"}</div>
        <div className="nav-scroll">{groups.map((group, i) => {
          const secondary = route.workspace === "id" && i === 3;
          const links = group.items.map(([id, title]) => <a key={id} href={`#/${route.workspace}/${id}`} onClick={() => { setMenuOpen(false); if (mobile) requestAnimationFrame(() => document.getElementById("main-content")?.focus()); }} className={`nav-item ${route.page === id ? "active" : ""}`} aria-current={route.page === id ? "page" : undefined}><Icon name={id} /><span>{id === "overview" ? "Обзор" : title}</span>{route.page === id && <span className="nav-active-dot" />}</a>);
          return secondary ? <details key={i} className="nav-block nav-reference" open={group.items.some(([id]) => id === route.page) || undefined}><summary><Icon name="registry" />Справочники<Icon name="chevron" size={14} /></summary>{links}</details> :
            <div className={`nav-block ${!group.title ? "nav-bottom" : ""}`} key={i}>{group.title && <div className="nav-group">{i === 0 && route.workspace === "id" ? "Рабочее пространство" : group.title}</div>}{links}</div>;
        })}</div>
        <div className="sidebar-footer"><div className="user-avatar">{user.full_name.split(" ").slice(0, 2).map((s) => s[0]).join("")}</div><div className="user-info"><span className="user-name">{user.full_name}</span><small>{orgName || "Текущая организация"}</small></div>
</div>
      </nav>
      <main className="main">
        <header className="app-header">
          <div className="header-location">{mobile && <button ref={menuButton} className="icon-button" aria-label="Открыть меню" aria-expanded={menuOpen} aria-controls="app-navigation" onClick={() => setMenuOpen(true)}><Icon name="menu" size={22} /></button>}
            <span className="header-workspace">{route.workspace === "id" ? "Рабочее пространство" : "Платформа"}</span><span className="header-divider">/</span><span className="header-page">{groups.flatMap((g) => g.items).find(([id]) => id === route.page)?.[1] ?? "Обзор"}</span></div>
          <div className="header-tools"><div className="project-switcher"><Icon name="project" size={17} /><label className="sr-only" htmlFor="active-project">Активный проект</label>
            <select id="active-project" value={project} onChange={(e) => setActiveProject(e.target.value)} title={objects.find((o) => o.id === project)?.name ?? "Выбрать проект"}>
              <option value="">Выбрать проект</option>{objects.map((o) => <option key={o.id} value={o.id}>{o.code ? o.code + " · " : ""}{o.name}</option>)}
            </select><Icon name="down" size={13} /></div>
            <button className="icon-button theme-toggle" aria-label={theme === "dark" ? "Светлая тема" : "Тёмная тема"} title={theme === "dark" ? "Светлая тема" : "Тёмная тема"} onClick={() => setTheme(theme === "dark" ? "light" : "dark")}><Icon name={theme === "dark" ? "sun" : "moon"} size={18} /></button>
          </div>
        </header>
        <div id="main-content" tabIndex={-1} className={`page-content ${route.page === "chat" ? "page-chat" : ""}`}>
        {DEMO && <div className="demo-banner">Демо-версия: данные вымышленные</div>}
        <Suspense fallback={<p className="muted">Загрузка рабочего пространства…</p>}>
          {projectError && <div className="alert error" role="alert">Не удалось получить проекты: {projectError} <button className="btn" onClick={() => void loadObjects()}>Повторить загрузку проектов</button></div>}
          <ExecutiveWorkspace key={`id:${user.organization_id}:${project}`} page={route.page} user={user} />
        </Suspense>
        </div>
      </main>
      {preview !== undefined && (
        <div className="modal-backdrop" onClick={() => setPreview(undefined)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <h2 style={{ margin: 0 }}>{preview?.title ?? "Предпросмотр"}</h2>
              <button className="btn small" onClick={() => setPreview(undefined)}>Закрыть</button>
            </div>
            {!preview && <p className="muted">Предпросмотр этого файла в демо-версии недоступен. На сервере файл скачивается в Word, Excel или DXF.</p>}
            {preview?.type === "svg" && <img alt="Предпросмотр" src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(preview.content)}`} style={{ width: "100%", background: "#fff" }} />}
            {preview?.type === "html" && <div className="doc-preview" dangerouslySetInnerHTML={{ __html: preview.content }} />}
            {preview && <p className="small muted">В демо-странице файлы не скачиваются; на сервере — кнопка «Скачать» отдаёт оригинал (DOCX, XLSX, DXF).</p>}
          </div>
        </div>
      )}
    </div>
  );
}

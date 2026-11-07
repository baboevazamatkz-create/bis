import { useEffect, useState } from "react";
import { api, DEMO, setToken, type User } from "./api";
import { previewFor } from "./demo/mock";
import Login from "./pages/Login";
import Feed from "./pages/Feed";
import Specialists from "./pages/Specialists";
import ArchiveImport from "./pages/ArchiveImport";
import Chat from "./pages/Chat";
import Control from "./pages/Control";
import Contracts from "./pages/Contracts";
import Works from "./pages/Works";
import Schemes from "./pages/Schemes";
import ProjectDocs from "./pages/ProjectDocs";
import Knowledge from "./pages/Knowledge";
import Documents from "./pages/Documents";
import AosrForm from "./pages/AosrForm";
import Registry, { REGISTRIES } from "./pages/Registry";
import Settings from "./pages/Settings";
import { Hint } from "./components";

const ROLE_TITLES: Record<string, string> = {
  admin: "Администратор", manager: "Руководитель", pto: "ПТО", foreman: "Прораб", supply: "Снабжение",
  lawyer: "Юрист", secretary: "Секретарь", viewer: "Наблюдатель",
};

const REGISTRY_GROUPS: [string, string[]][] = [
  ["Реестры", ["objects", "counterparties", "contracts", "persons", "appointments", "tasks", "obligations"]],
  ["Учёт работ и материалов", ["estimate", "schedule", "volumes", "materials", "norms", "receipts", "usage"]],
];

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [preview, setPreview] = useState<{ type: string; content: string; title: string } | null | undefined>(undefined);
  const [page, setPage] = useState("feed");
  const [unread, setUnread] = useState(0);
  const [pendingAI, setPendingAI] = useState(0);
  const [dataOpen, setDataOpen] = useState(() => {
    try { return localStorage.getItem("bis_nav_data") === "1"; } catch { return false; }
  });
  const toggleData = () => {
    setDataOpen((v) => {
      try { localStorage.setItem("bis_nav_data", v ? "0" : "1"); } catch { /* необязательно */ }
      return !v;
    });
  };

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener("bis:logout", onLogout);
    const onPreview = (e: Event) => setPreview(previewFor((e as CustomEvent<string>).detail));
    window.addEventListener("bis:preview", onPreview);
    // без сохранённого входа тоже спрашиваем: на своём компьютере BIS пускает без пароля (BIS_AUTO_LOGIN)
    api<{ user: User }>("/auth/me")
      .then((r) => setUser(r.user))
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
    return () => {
      window.removeEventListener("bis:logout", onLogout);
      window.removeEventListener("bis:preview", onPreview);
    };
  }, []);

  useEffect(() => {
    if (!user) return;
    const poll = () => {
      api<{ notifications_unread: number }>("/control/summary").then((s) => setUnread(s.notifications_unread)).catch(() => {});
      api<{ pending: number }>("/ai/feed?limit=1").then((r) => setPendingAI(r.pending)).catch(() => {});
    };
    poll();
    const t = setInterval(poll, 60000);
    return () => clearInterval(t);
  }, [user, page]);

  if (loading) return <div className="login muted">Загрузка…</div>;
  if (!user && DEMO) return <div className="login muted">Демо-данные не загружены</div>;
  if (!user) return <Login onLogin={setUser} />;

  const nav = (id: string, label: string, badge?: number) => (
    <button key={id} className={`nav-item ${page === id ? "active" : ""}`} onClick={() => setPage(id)}>
      {label}
      {!!badge && <span className="nav-badge">{badge}</span>}
    </button>
  );

  let content;
  switch (page) {
    case "feed": content = <Feed onPending={setPendingAI} />; break;
    case "specialists": content = <Specialists user={user} />; break;
    case "archive": content = <ArchiveImport />; break;
    case "chat": content = <Chat />; break;
    case "control": content = <Control />; break;
    case "contracts": content = <Contracts />; break;
    case "works": content = <Works />; break;
    case "schemes": content = <Schemes />; break;
    case "project": content = <ProjectDocs />; break;
    case "knowledge": content = <Knowledge user={user} />; break;
    case "documents": content = <Documents />; break;
    case "aosr": content = <AosrForm onCreated={() => setPage("documents")} />; break;
    case "settings": content = <Settings />; break;
    default: content = <Registry key={page} kind={page.split(":")[1]} />;
  }

  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">
          BIS<small>строительная ИИ-платформа</small>
        </div>
        <div className="nav-block">
          {nav("feed", "Лента ИИ", pendingAI)}
          {nav("chat", "Ассистент")}
          {nav("control", "Контроль", unread)}
        </div>
        <div className="nav-block">
          <div className="nav-group">Документы</div>
          {nav("documents", "Документы")}
          {nav("schemes", "Исполнительные схемы")}
          {nav("contracts", "Договоры")}
          {nav("project", "Проект")}
          {nav("works", "Работы и закрытие")}
        </div>
        <div className="nav-block">
          <button className="nav-group nav-toggle" onClick={toggleData} aria-expanded={dataOpen}>
            {dataOpen ? "▾" : "▸"} Данные
          </button>
          {dataOpen && (
            <>
              {nav("knowledge", "База знаний")}
              {nav("aosr", "АОСР вручную")}
              {REGISTRY_GROUPS.map(([group, keys]) => (
                <div key={group} className="nav-section">
                  <div className="nav-group sub">{group}</div>
                  {keys.map((k) => nav(`registry:${k}`, REGISTRIES[k].title))}
                </div>
              ))}
            </>
          )}
        </div>
        <div className="nav-block nav-bottom">
          {nav("specialists", "ИИ-специалисты")}
          {nav("archive", "Загрузка архива")}
          {nav("settings", "Настройки")}
        </div>
        <div className="sidebar-footer">
          <span className="user-name" title={ROLE_TITLES[user.role] ?? user.role}>{user.full_name}</span>
          <a href="#" onClick={(e) => { e.preventDefault(); setToken(null); setUser(null); }}>Выйти</a>
        </div>
      </nav>
      <main className="main">
        {DEMO && (
          <div className="demo-banner">
            Демо-версия: данные вымышленные
            <Hint wide>Изменения живут до перезагрузки страницы. Сообщения, фото и текстовые файлы в «Ленте ИИ» разбирает ИИ вживую
              (в демо — Claude, на сервере — YandexGPT); голос — распознавание браузера.</Hint>
          </div>
        )}
        {content}
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

import { useEffect, useState } from "react";
import { api, getToken, setToken, type User } from "./api";
import Login from "./pages/Login";
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
  const [loading, setLoading] = useState(!!getToken());
  const [page, setPage] = useState("chat");
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    const onLogout = () => setUser(null);
    window.addEventListener("bis:logout", onLogout);
    if (getToken()) {
      api<{ user: User }>("/auth/me")
        .then((r) => setUser(r.user))
        .catch(() => setToken(null))
        .finally(() => setLoading(false));
    }
    return () => window.removeEventListener("bis:logout", onLogout);
  }, []);

  useEffect(() => {
    if (!user) return;
    const poll = () => api<{ notifications_unread: number }>("/control/summary").then((s) => setUnread(s.notifications_unread)).catch(() => {});
    poll();
    const t = setInterval(poll, 60000);
    return () => clearInterval(t);
  }, [user, page]);

  if (loading) return <div className="login muted">Загрузка…</div>;
  if (!user) return <Login onLogin={setUser} />;

  const nav = (id: string, label: string, badge?: number) => (
    <button key={id} className={`nav-item ${page === id ? "active" : ""}`} onClick={() => setPage(id)}>
      {label}
      {!!badge && <span className="nav-badge">{badge}</span>}
    </button>
  );

  let content;
  switch (page) {
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
        {nav("chat", "Ассистент")}
        {nav("control", "Контроль", unread)}
        {nav("documents", "Документы")}
        <div className="nav-group">Работа</div>
        {nav("aosr", "Новый АОСР")}
        {nav("contracts", "Договоры")}
        {nav("works", "Работы и закрытие")}
        {nav("schemes", "Исполнительные схемы")}
        {nav("project", "Проектная документация")}
        {nav("knowledge", "База знаний")}
        {REGISTRY_GROUPS.map(([group, keys]) => (
          <div key={group} className="nav-section">
            <div className="nav-group">{group}</div>
            {keys.map((k) => nav(`registry:${k}`, REGISTRIES[k].title))}
          </div>
        ))}
        <div className="nav-group">Организация</div>
        {nav("settings", "Настройки")}
        <div className="sidebar-footer">
          {user.full_name}
          <br />
          <span className="muted">{ROLE_TITLES[user.role] ?? user.role}</span>
          <br />
          <a href="#" onClick={(e) => { e.preventDefault(); setToken(null); setUser(null); }}>Выйти</a>
        </div>
      </nav>
      <main className="main">{content}</main>
    </div>
  );
}

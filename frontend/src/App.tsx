import { useEffect, useState } from "react";
import { api, getToken, setToken, type User } from "./api";
import Login from "./pages/Login";
import Chat from "./pages/Chat";
import Knowledge from "./pages/Knowledge";
import Documents from "./pages/Documents";
import AosrForm from "./pages/AosrForm";
import Registry, { REGISTRIES } from "./pages/Registry";
import Settings from "./pages/Settings";

type Page = "chat" | "knowledge" | "documents" | "aosr" | "settings" | `registry:${string}`;

const ROLE_TITLES: Record<string, string> = {
  admin: "Администратор", manager: "Руководитель", pto: "ПТО", foreman: "Прораб", supply: "Снабжение",
  lawyer: "Юрист", secretary: "Секретарь", viewer: "Наблюдатель",
};

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(!!getToken());
  const [page, setPage] = useState<Page>("chat");

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

  if (loading) return <div className="login muted">Загрузка…</div>;
  if (!user) return <Login onLogin={setUser} />;

  const nav = (id: Page, label: string) => (
    <button className={`nav-item ${page === id ? "active" : ""}`} onClick={() => setPage(id)}>
      {label}
    </button>
  );

  let content;
  if (page === "chat") content = <Chat />;
  else if (page === "knowledge") content = <Knowledge user={user} />;
  else if (page === "documents") content = <Documents />;
  else if (page === "aosr") content = <AosrForm onCreated={() => setPage("documents")} />;
  else if (page === "settings") content = <Settings />;
  else content = <Registry key={page} kind={page.split(":")[1]} />;

  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">
          BIS<small>строительная ИИ-платформа</small>
        </div>
        {nav("chat", "Ассистент")}
        {nav("documents", "Документы")}
        {nav("aosr", "Новый АОСР")}
        {nav("knowledge", "База знаний")}
        <div className="nav-group">Реестры</div>
        {Object.entries(REGISTRIES).map(([k, r]) => nav(`registry:${k}`, r.title))}
        <div className="nav-group">Организация</div>
        {nav("settings", "Настройки")}
        <div className="sidebar-footer">
          {user.full_name}
          <br />
          <span className="muted">{ROLE_TITLES[user.role] ?? user.role}</span>
          <br />
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              setToken(null);
              setUser(null);
            }}
          >
            Выйти
          </a>
        </div>
      </nav>
      <main className="main">{content}</main>
    </div>
  );
}

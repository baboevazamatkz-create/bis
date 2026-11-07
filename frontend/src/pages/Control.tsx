import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, date, Stat, useAsync } from "../components";

interface Summary {
  obligations_overdue: number;
  obligations_week: number;
  obligations_no_responsible: number;
  tasks_overdue: number;
  tasks_open: number;
  notifications_unread: number;
}
interface Notification {
  id: string; created_at: string; level: string; title: string; body: string | null; is_read: boolean;
}
interface Obligation {
  id: string; contract_id: string; description: string; party: string; kind: string; due_date: string | null;
  due_text: string | null; status: string; penalty: string | null; source_clause: string | null;
}
interface Task {
  id: string; title: string; due_date: string | null; status: string; priority: string;
}
interface Job { name: string; title: string; last_run: string | null; status: string | null }

const today = () => new Date().toISOString().slice(0, 10);

export default function Control() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [notes, setNotes] = useState<Notification[]>([]);
  const [obligations, setObligations] = useState<Obligation[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [contracts, setContracts] = useState<Record<string, string>>({});
  const { run, Alerts } = useAsync();

  const load = () => {
    api<Summary>("/control/summary").then(setSummary);
    api<Notification[]>("/notifications?limit=30").then(setNotes);
    api<Obligation[]>("/obligations?limit=500").then((rows) =>
      setObligations(rows.filter((o) => ["open", "overdue"].includes(o.status))
        .sort((a, b) => (a.due_date ?? "9999").localeCompare(b.due_date ?? "9999"))));
    api<Task[]>("/tasks?limit=500").then((rows) => setTasks(rows.filter((t) => ["open", "in_progress"].includes(t.status))
      .sort((a, b) => (a.due_date ?? "9999").localeCompare(b.due_date ?? "9999"))));
    api<{ id: string; number: string }[]>("/contracts").then((rows) =>
      setContracts(Object.fromEntries(rows.map((c) => [c.id, c.number]))));
    api<Job[]>("/jobs").then(setJobs).catch(() => setJobs([]));
  };
  useEffect(load, []);

  const setTask = (t: Task, status: string) => run(() => api(`/tasks/${t.id}`, { method: "PATCH", json: { status } })).then(load);
  const setOb = (o: Obligation, status: string) =>
    run(() => api(`/obligations/${o.id}`, { method: "PATCH", json: { status } })).then(load);

  const overdue = (d: string | null) => !!d && d < today();

  return (
    <>
      <h1>Контроль исполнения</h1>
      <Alerts />
      {summary && (
        <div className="stats">
          <Stat label="Просрочено обязательств" value={summary.obligations_overdue} tone={summary.obligations_overdue ? "bad" : "ok"} />
          <Stat label="Сроки на этой неделе" value={summary.obligations_week} tone={summary.obligations_week ? "warn" : undefined} />
          <Stat label="Без ответственного" value={summary.obligations_no_responsible} tone={summary.obligations_no_responsible ? "warn" : undefined} />
          <Stat label="Поручений просрочено" value={summary.tasks_overdue} tone={summary.tasks_overdue ? "bad" : "ok"} />
          <Stat label="Поручений в работе" value={summary.tasks_open} />
          <Stat label="Новых уведомлений" value={summary.notifications_unread} />
        </div>
      )}

      <div className="grid2">
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2>Уведомления</h2>
            <button className="btn small" onClick={() => run(() => api("/notifications/read-all", { method: "POST" })).then(load)}>
              Прочитать все
            </button>
          </div>
          {notes.length === 0 && <p className="muted">Уведомлений нет</p>}
          {notes.map((n) => (
            <div key={n.id} className={`note ${n.level} ${n.is_read ? "read" : ""}`}>
              <div className="small muted">{new Date(n.created_at).toLocaleString("ru-RU")}</div>
              <b>{n.title}</b>
              {n.body && <div className="small" style={{ whiteSpace: "pre-wrap" }}>{n.body}</div>}
            </div>
          ))}
        </div>

        <div className="panel">
          <h2>Поручения</h2>
          <table>
            <tbody>
              {tasks.length === 0 && <tr><td className="muted">Открытых поручений нет</td></tr>}
              {tasks.map((t) => (
                <tr key={t.id}>
                  <td>
                    {t.priority === "high" && <span className="badge t-bad">срочно</span>} {t.title}
                    <div className={`small ${overdue(t.due_date) ? "text-bad" : "muted"}`}>срок {date(t.due_date)}</div>
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {t.status === "open" && <button className="btn small" onClick={() => setTask(t, "in_progress")}>В работу</button>}{" "}
                    <button className="btn small primary" onClick={() => setTask(t, "done")}>Исполнено</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <h2>Обязательства по договорам</h2>
        <table>
          <thead>
            <tr><th>Срок</th><th>Обязательство</th><th>Сторона</th><th>Договор</th><th>Статус</th><th /></tr>
          </thead>
          <tbody>
            {obligations.map((o) => (
              <tr key={o.id}>
                <td className={overdue(o.due_date) ? "text-bad" : ""}>{o.due_date ? date(o.due_date) : <span className="small muted">{o.due_text ?? "—"}</span>}</td>
                <td>
                  {o.description}
                  {o.penalty && <div className="small muted">Ответственность: {o.penalty.slice(0, 160)}</div>}
                </td>
                <td>{o.party === "we" ? "мы" : "контрагент"}</td>
                <td className="small">№ {contracts[o.contract_id] ?? "…"} {o.source_clause ?? ""}</td>
                <td><Badge status={o.status} /></td>
                <td><button className="btn small" onClick={() => setOb(o, "done")}>Исполнено</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {jobs.length > 0 && (
        <div className="panel">
          <h2>Фоновые проверки</h2>
          <table>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.name}>
                  <td>{j.title}</td>
                  <td className="small muted">{j.last_run ? new Date(j.last_run).toLocaleString("ru-RU") : "не запускалась"}</td>
                  <td>{j.status && <Badge status={j.status === "ok" ? "ok" : "overdue"} />}</td>
                  <td>
                    <button className="btn small" onClick={() => run(() => api(`/jobs/${j.name}/run`, { method: "POST" }), "Проверка выполнена").then(load)}>
                      Запустить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

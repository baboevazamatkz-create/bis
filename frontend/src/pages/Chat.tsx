import { useEffect, useRef, useState } from "react";
import { api, download, type ChatReply, type Citation, type DocRef } from "../api";

interface Msg {
  role: "user" | "assistant";
  content: string;
  agent?: string | null;
  meta?: Partial<ChatReply> & { routed_by?: string };
}

interface Agent {
  name: string;
  title: string;
  description: string;
}

const SUGGESTIONS = [
  "Какая допустимая толщина слоя обратной засыпки?",
  "Сделай АОСР на армирование фундаментной плиты в осях 1-5, корпус 1",
  "Подготовь письмо заказчику о направлении исполнительной документации",
  "Какие договоры есть по корпусу 1?",
];

function Citations({ items }: { items: Citation[] }) {
  if (!items?.length) return null;
  return (
    <div className="citations">
      <div className="muted">Источники:</div>
      {items.map((c, i) => (
        <details key={c.chunk_id}>
          <summary>
            [{i + 1}] {c.citation}
          </summary>
          <div className="small" style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>
            {c.section_path && <div className="muted">{c.section_path}</div>}
            {c.content}
          </div>
        </details>
      ))}
    </div>
  );
}

function Docs({ items }: { items: DocRef[] }) {
  if (!items?.length) return null;
  return (
    <div>
      {items.map((d) => (
        <span className="doc-chip" key={d.id}>
          {d.title}
          <button className="btn small" onClick={() => download(`/documents/${d.id}/file`)}>
            Скачать
          </button>
        </span>
      ))}
    </div>
  );
}

export default function Chat() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState("");
  const [sessions, setSessions] = useState<{ id: string; title: string }[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  const loadSessions = () => api<{ id: string; title: string }[]>("/chat/sessions").then(setSessions).catch(() => {});

  useEffect(() => {
    api<Agent[]>("/agents").then(setAgents).catch(() => {});
    loadSessions();
  }, []);

  useEffect(() => {
    // в новых браузерах scrollIntoView возвращает Promise — не отдаём его React как функцию очистки
    void bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  async function openSession(id: string) {
    setSessionId(id);
    setMessages(await api<Msg[]>(`/chat/sessions/${id}`));
  }

  async function send(message: string) {
    if (!message.trim() || busy) return;
    setError("");
    setText("");
    setMessages((m) => [...m, { role: "user", content: message }]);
    setBusy(true);
    try {
      const r = await api<ChatReply>("/chat", {
        method: "POST",
        json: { message, session_id: sessionId, agent: agent || null },
      });
      setSessionId(r.session_id);
      setMessages((m) => [...m, { role: "assistant", content: r.text, agent: r.agent, meta: r }]);
      if (!sessionId) loadSessions();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const agentTitle = (name?: string | null) => agents.find((a) => a.name === name)?.title ?? name;

  return (
    <div className="chat">
      <div className="panel chat-sessions">
        <button
          className="btn primary"
          style={{ width: "100%", marginBottom: 10 }}
          onClick={() => {
            setSessionId(null);
            setMessages([]);
          }}
        >
          Новый диалог
        </button>
        {sessions.map((s) => (
          <button key={s.id} className={`btn small ${s.id === sessionId ? "primary" : ""}`} onClick={() => openSession(s.id)}>
            {s.title}
          </button>
        ))}
      </div>
      <div className="chat-main">
        <div className="row" style={{ marginBottom: 8 }}>
          <div className="field" style={{ maxWidth: 320 }}>
            <label>Агент</label>
            <select value={agent} onChange={(e) => setAgent(e.target.value)}>
              <option value="">Автоматически (супервизор)</option>
              {agents.map((a) => (
                <option key={a.name} value={a.name} title={a.description}>
                  {a.title}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="messages">
          {messages.length === 0 && (
            <div className="panel">
              <h2>Чем помочь?</h2>
              <p className="muted">
                Вопросы по нормам с цитатами, черновики АОСР и писем, поиск по договорам и документам. Документы создаются
                черновиками — утверждает человек.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="btn small" onClick={() => send(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <div className="bubble">{m.content}</div>
              {m.role === "assistant" && (
                <>
                  <Docs items={m.meta?.documents ?? []} />
                  <Citations items={m.meta?.citations ?? []} />
                  <div className="meta">
                    {agentTitle(m.agent)}
                    {m.meta?.routed_by ? ` · ${m.meta.routed_by}` : ""}
                    {m.meta?.provider ? ` · ${m.meta.provider}` : ""}
                    {m.meta?.trace?.length ? ` · инструменты: ${m.meta.trace.map((t) => t.tool).join(", ")}` : ""}
                  </div>
                </>
              )}
            </div>
          ))}
          {busy && <div className="msg assistant muted">Агент работает…</div>}
          <div ref={bottom} />
        </div>
        {error && <div className="alert error">{error}</div>}
        <div className="composer">
          <textarea
            value={text}
            placeholder="Сообщение… (Enter — отправить, Shift+Enter — новая строка)"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(text);
              }
            }}
          />
          <button className="btn primary" disabled={busy || !text.trim()} onClick={() => send(text)}>
            Отправить
          </button>
        </div>
      </div>
    </div>
  );
}

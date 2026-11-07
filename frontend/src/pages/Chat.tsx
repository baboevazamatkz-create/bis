import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, download, fetchBlob, openFile, type ChatReply, type Citation, type DocRef } from "../api";

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
  "Что просрочено по договорам и поручениям?",
  "Покажи план-факт по корпусу 1",
  "Какой перерасход материалов за май 2026?",
  "Что можно закрыть в КС-2 за май 2026 по договору ГП-01/2026?",
  "Сделай АОСР на армирование фундаментной плиты в осях 1-5, корпус 1",
  "Подготовь письмо заказчику о направлении исполнительной документации",
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
    <div className="doc-cards">
      {items.map((d) => (
        <span className="doc-chip" key={d.id}>
          <span className="doc-chip-title" title={d.title}>{d.title}</span>
          <button className="btn small" onClick={() => void openFile(`/documents/${d.id}/file`, d.title)}>Открыть</button>
          <button className="btn small" onClick={() => void download(`/documents/${d.id}/file`, d.title)} title="Скачать">⬇</button>
        </span>
      ))}
    </div>
  );
}

const DOC_LINK = /^(?:document:|\/?(?:api\/)?documents\/)([0-9a-f-]{36})(?:\/file)?$/i;
const MD = /\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*/g;

/** Текст ответа: ссылки на документы — кнопки «открыть», внешние — ссылки, **жирный** — жирный. */
function Rich({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(MD)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    const [, label, url, bold] = m;
    if (bold) out.push(<b key={m.index}>{bold}</b>);
    else if (DOC_LINK.test(url)) {
      const id = DOC_LINK.exec(url)![1];
      out.push(<button key={m.index} className="link" onClick={() => void openFile(`/documents/${id}/file`, label)}>{label}</button>);
    } else if (/^https?:\/\//.test(url)) {
      out.push(<a key={m.index} href={url} target="_blank" rel="noopener noreferrer">{label}</a>);
    } else out.push(label);
    last = m.index! + m[0].length;
  }
  out.push(text.slice(last));
  return <>{out}</>;
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
  const [voice, setVoice] = useState({ stt: false, tts: false });
  const [speakReplies, setSpeakReplies] = useState(false);
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);

  const loadSessions = () => api<{ id: string; title: string }[]>("/chat/sessions").then(setSessions).catch(() => {});

  useEffect(() => {
    api<Agent[]>("/agents").then(setAgents).catch(() => {});
    api<{ stt: boolean; tts: boolean }>("/voice/status").then(setVoice).catch(() => {});
    loadSessions();
  }, []);

  async function speak(textToSpeak: string) {
    try {
      const blob = await fetchBlob("/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: textToSpeak.slice(0, 4000) }),
      });
      void new Audio(URL.createObjectURL(blob)).play();
    } catch {
      /* озвучивание необязательно */
    }
  }

  async function toggleRecording() {
    if (recording) {
      recorder.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        const form = new FormData();
        form.append("file", new Blob(chunks, { type: rec.mimeType }), "voice.webm");
        try {
          const r = await api<{ text: string }>("/voice/transcribe", { method: "POST", body: form });
          if (r.text.trim()) send(r.text);
        } catch (e) {
          setError((e as Error).message);
        }
      };
      rec.start();
      recorder.current = rec;
      setRecording(true);
    } catch {
      setError("Нет доступа к микрофону");
    }
  }

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
      if (speakReplies) speak(r.text);
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
          {voice.tts && (
            <div className="field" style={{ flex: "0 0 auto" }}>
              <label>
                <input type="checkbox" style={{ width: "auto" }} checked={speakReplies} onChange={(e) => setSpeakReplies(e.target.checked)} /> Озвучивать ответы
              </label>
            </div>
          )}
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
              <div className="bubble">{m.role === "assistant" ? <Rich text={m.content} /> : m.content}</div>
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
          {voice.stt && (
            <button className={`btn ${recording ? "recording" : ""}`} title="Голосовая команда" onClick={toggleRecording} disabled={busy}>
              {recording ? "■ Стоп" : "🎤"}
            </button>
          )}
          <button className="btn primary" disabled={busy || !text.trim()} onClick={() => send(text)}>
            Отправить
          </button>
        </div>
      </div>
    </div>
  );
}

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, download, fetchBlob, openFile, streamChat, type ChatReply, type Citation, type DocRef } from "../api";
import Icon from "../components/Icon";
import { PTO_SUGGESTIONS } from "../modules/executive/config";
import { takeAssistantTask } from '../modules/executive/assistantTask';

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

function Citations({ items, onError }: { items: Citation[]; onError: (message: string) => void }) {
  if (!items?.length) return null;
  return (
    <div className="citations">
      <div className="muted">Источники:</div>
      {items.map((c, i) => (
        <details key={c.chunk_id}>
          <summary>
            [{i + 1}] {c.citation}
            {(c.fact_snapshot_id || c.document_id || c.source_id) && (
              <button className="link small" onClick={(e) => { e.preventDefault(); void openFile(c.fact_snapshot_id ? `/brain/facts/snapshots/${c.fact_snapshot_id}/file${c.object_id ? `?object_id=${c.object_id}` : ""}` : c.document_id ? `/documents/${c.document_id}/file` : `/knowledge/sources/${c.source_id}/file`, c.title, c.page).catch((e) => onError(e.message)); }}>
                {c.page ? `открыть стр. ${c.page}` : "открыть"}
              </button>
            )}
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

function Docs({ items, onError }: { items: DocRef[]; onError: (message: string) => void }) {
  if (!items?.length) return null;
  return (
    <div className="doc-cards">
      {items.map((d) => (
        <span className="doc-chip" key={d.id}>
          <span className="doc-chip-title" title={d.title}>{d.title}</span>
          <button className="btn small" onClick={() => void openFile(`/documents/${d.id}/file`, d.title).catch((e) => onError(e.message))}>Открыть</button>
          <button className="btn small" onClick={() => void download(`/documents/${d.id}/file`, d.title).catch((e) => onError(e.message))} title="Скачать">⬇</button>
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

export default function Chat({ workspace = "platform" }: { workspace?: "platform" | "executive" }) {
  const [showHistory, setShowHistory] = useState(false);
  const executive = workspace === "executive";
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState(executive ? "assistant" : "");
  const [documentId, setDocumentId] = useState<string | null>(null);
  const [documentTitle, setDocumentTitle] = useState('');
  const [sessions, setSessions] = useState<{ id: string; title: string }[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState<string[]>([]);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const [voice, setVoice] = useState({ stt: false, tts: false });
  const [speakReplies, setSpeakReplies] = useState(false);
  const [recording, setRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => { const task = takeAssistantTask(); if (task) {
      setDocumentId(task.document_id ?? null); setDocumentTitle(task.title ?? '');
      void send(task.text, task.document_id);
    } }, 0);
    return () => clearTimeout(timer);
  }, []);

  const loadSessions = () => api<{ id: string; title: string }[]>(`/chat/sessions?workspace=${workspace}`).then(setSessions).catch(() => {});

  useEffect(() => {
    api<Agent[]>("/agents").then((list) => setAgents(executive ? list.filter((a) => ["pto", "cad", "norms", "assistant"].includes(a.name)) : list)).catch(() => {});
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
          const r = await api<{ text: string }>("/voice/transcribe", { method: "POST", body: form, timeoutMs: 120_000 });
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
    try {
      const messages = await api<Msg[]>(`/chat/sessions/${id}?workspace=${workspace}`);
      setSessionId(id); setMessages(messages);
    } catch (e) { setError((e as Error).message); }
  }

  async function send(message: string, selectedDocument?: string) {
    if (!message.trim() || busy) return;
    setError("");
    setText("");
    setMessages((m) => [...m, { role: "user", content: message }]);
    setBusy(true);
    setSteps([]);
    const controller = new AbortController();
    pending.current = controller;
    try {
      const r = await streamChat<ChatReply>({ message, session_id: sessionId, agent: agent || null, workspace,
        document_id: selectedDocument || documentId || null }, (e) => {
        if (e.type === "route" && e.title) setSteps((s) => [...s, `Отвечает: ${e.title}`]);
        if (e.type === "step" && e.label) setSteps((s) => [...s, e.label!]);
      }, { signal: controller.signal });
      setSessionId(r.session_id);
      setMessages((m) => [...m, { role: "assistant", content: r.text, agent: r.agent, meta: r }]);
      if (speakReplies) speak(r.text);
      if (!sessionId) loadSessions();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      pending.current = null;
      setBusy(false);
      setSteps([]);
    }
  }

  const agentTitle = (name?: string | null) => agents.find((a) => a.name === name)?.title ?? name;

  return (
    <div className={`chat ${showHistory ? "history-open" : ""}`}>
      <div className="chat-sessions"><div className="chat-history-heading">ДИАЛОГИ</div>
        <button
          className="btn new-chat"
          disabled={busy}
          style={{ width: "100%", marginBottom: 16 }}
          onClick={() => {
            setSessionId(null);
            setMessages([]);
            setDocumentId(null); setDocumentTitle('');
            setError("");
            setShowHistory(false);
          }}
        >
          <Icon name="plus" size={16} /> Новый диалог
        </button>
        {sessions.length === 0 && <p className="history-empty">Здесь будут ваши<br />диалоги с Синапсом.</p>}
        {sessions.map((s) => (
          <button key={s.id} className={`session-item ${s.id === sessionId ? "selected" : ""}`} disabled={busy} title={s.title} onClick={() => { void openSession(s.id); setShowHistory(false); }}>
            {s.title}
          </button>
        ))}
      </div>
      <div className="chat-main">
        <div className="chat-toolbar">
          <div className="chat-identity"><span className="synapse-avatar"><Icon name="spark" size={18} /></span><div><strong>Синапс</strong><span>Помощник {executive ? "инженера ПТО" : "по проекту"}</span></div></div>
          <button className="btn small chat-history-toggle" aria-expanded={showHistory} onClick={() => setShowHistory(!showHistory)}><Icon name="chat" size={15} />{showHistory ? "Закрыть историю" : "История"}</button>
          {!executive && <div className="field agent-picker">
            <label className="sr-only" htmlFor="chat-agent">Специалист</label>
            <select id="chat-agent" aria-label="Специалист" value={agent} onChange={(e) => setAgent(e.target.value)}>
              {!executive && <option value="">Автоматически (супервизор)</option>}
              {agents.map((a) => (
                <option key={a.name} value={a.name} title={a.description}>
                  {a.title}
                </option>
              ))}
            </select>
          </div>}
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
            <div className="chat-welcome">
              <div className="welcome-symbol"><Icon name="spark" size={30} /></div><span className="welcome-eyebrow">ВАШ ПРОЕКТ. ВЕСЬ КОНТЕКСТ.</span>
              <h2>С чего начнём?</h2>
              <p className="muted">
                {executive ? "Помогу собрать акт, найти документ или проверить комплект. Начните с вопроса или выберите задачу."
                  : "Найду основания в документах, подготовлю черновик или помогу разобраться в проекте."}
              </p>
              <div className="suggestions">
                {(executive ? PTO_SUGGESTIONS : SUGGESTIONS).slice(0, 4).map((s, index) => (
                  <button key={s} className="suggestion-card" onClick={() => send(s)}><Icon name={["check", "documents", "project", "sources"][index]} size={19} />
                    <span>{s}</span><Icon name="arrow" size={16} />
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
                  <Docs items={m.meta?.documents ?? []} onError={setError} />
                  <Citations items={m.meta?.citations ?? []} onError={setError} />
                  {(m.meta?.provider === "offline" || m.meta?.model === "offline") && <p className="small text-bad">Ответ получен без языковой модели. Проверьте подключение ИИ; поиск по документам доступен отдельно в разделе «Синапс».</p>}
                  <div className="meta">{agentTitle(m.agent) || "Синапс"}
                    {(m.meta?.provider || !!m.meta?.trace?.length) && <details className="chat-tech"><summary>Детали ответа</summary><p>{m.meta?.provider}{m.meta?.routed_by ? ` · ${m.meta.routed_by}` : ""}</p>{m.meta?.trace?.map((t, n) => <span key={n}>{t.tool}{t.error ? " · ошибка" : ""}<br /></span>)}</details>}
                  </div>
                </>
              )}
            </div>
          ))}
          {busy && (
            <div className="msg assistant">
              <div className="bubble steps">
                {(steps.length ? steps : ["Разбираю вопрос"]).map((x, i, all) => (
                  <div key={i} className={i === all.length - 1 ? "step now" : "step"}>{i === all.length - 1 ? "⏳" : "✓"} {x}</div>
                ))}
                <button className="btn small" onClick={() => pending.current?.abort()}>Остановить запрос</button>
              </div>
            </div>
          )}
          <div ref={bottom} />
        </div>
        {error && <div className="alert error">{error}</div>}
        <div className="chat-compose-area"><div className="composer">
          <textarea
            aria-label="Сообщение Синапсу"
            rows={2}
            value={text}
            placeholder="Спросите о проекте или опишите выполненную работу…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send(text);
              }
            }}
          />
          {(voice.stt || executive) && (
            <button className={`btn ${recording ? "recording" : ""}`} title="Голосовая команда" aria-label="Голосовая команда" onClick={() => voice.stt ? void toggleRecording() : setError('Голос не подключён. Откройте «Базы данных» → «ИИ и качество чтения» и настройте распознавание.')} disabled={busy}>
              {recording ? "Стоп" : <Icon name="mic" />}
            </button>
          )}
          <button className="btn primary" aria-label="Отправить сообщение" disabled={busy || !text.trim()} onClick={() => send(text)}>
            <span>Отправить</span><Icon name="arrow" size={18} />
          </button>
        </div>{documentTitle && <div className="small muted">Документ: {documentTitle}</div>}<div className="composer-caption"><span>Основания — в ваших документах. Решение — за вами.</span><span className="composer-keyboard">Enter ↵</span></div></div>
      </div>
    </div>
  );
}

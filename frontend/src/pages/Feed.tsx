// «Лента ИИ» — главный экран: всё, что приходит с объектов, и что с этим сделали ИИ-специалисты.
// Человек не заполняет формы — он принимает решения: принять, исправить, отклонить.
import { useEffect, useRef, useState } from "react";
import { api, DEMO, download, fetchBlob, type AIAction, type IntakeEvent } from "../api";
import { useObjects, useOptions } from "../components";

const SOURCE: Record<string, string> = { telegram: "Telegram", email: "почта", upload: "загрузка", user: "запрос", api: "API" };
const KIND: Record<string, string> = { photo: "Фото", voice: "Голосовое", document: "Документ", text: "Сообщение" };
const KIND_ICON: Record<string, string> = { photo: "📷", voice: "🎙", document: "📄", text: "💬" };
const BY: Record<string, string> = { rules: "правила", user: "пользователь", vision: "модель зрения" };
const FIELD_LABELS: Record<string, string> = {
  material_id: "Материал", estimate_item_id: "Позиция сметы", object_id: "Объект", quantity: "Количество", price: "Цена за ед.",
  date: "Дата", quality_doc: "Документ о качестве", location: "Место (оси, захватка)", title: "Формулировка", role: "Кому",
  due_days: "Срок, дней", priority: "Приоритет", works: "Работы", project_docs: "Рабочая документация", materials: "Материалы",
  executive_docs: "Исполнительные документы", norms: "Нормы", next_works: "Разрешаются работы", kind: "Вид договора",
  our_role: "Наша роль", receipt_ids: "Поступления", text: "Текст",
};
const LIST_FIELDS = ["materials", "executive_docs", "norms"];
const ROLE_OPTIONS = [["manager", "Руководитель участка"], ["pto", "ПТО"], ["supply", "Снабжение"], ["foreman", "Прораб"],
  ["lawyer", "Юрист"], ["secretary", "Секретарь"]];

const time = (v: string | null) => (v ? new Date(v).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
const pct = (v: number) => `${Math.round(v * 100)}%`;
const apiPath = (url: string) => url.replace(/^\/api/, "");

interface Sample { id: string; title: string; hint: string }

export default function Feed({ onPending }: { onPending?: (n: number) => void }) {
  const [events, setEvents] = useState<IntakeEvent[]>([]);
  const [pending, setPending] = useState(0);
  const [only, setOnly] = useState<"all" | "pending" | "issues">("all");
  const [objectId, setObjectId] = useState("");
  const [loading, setLoading] = useState(true);
  const objects = useObjects();

  const load = () => {
    const q = new URLSearchParams({ only, limit: "40" });
    if (objectId) q.set("object_id", objectId);
    return api<{ events: IntakeEvent[]; pending: number }>(`/ai/feed?${q}`)
      .then((r) => { setEvents(r.events); setPending(r.pending); onPending?.(r.pending); })
      .finally(() => setLoading(false));
  };
  useEffect(() => { void load(); }, [only, objectId]); // eslint-disable-line react-hooks/exhaustive-deps

  const replace = (ev: IntakeEvent) => setEvents((list) => list.map((e) => (e.id === ev.id ? ev : e)));

  return (
    <>
      <div className="feed-head">
        <div>
          <h1>Лента ИИ</h1>
          <p className="muted feed-lead">
            Фото, голосовые, документы и сообщения с объектов. ИИ-специалисты разбирают их и делают работу сами —
            вам остаются решения там, где нужен человек.
          </p>
        </div>
      </div>

      <Composer objects={objects} onSent={(ev) => { setEvents((list) => [ev, ...list]); void load(); }} />

      <div className="feed-filters">
        <div className="tabs">
          <button className={`tab ${only === "all" ? "active" : ""}`} onClick={() => setOnly("all")}>Всё</button>
          <button className={`tab ${only === "pending" ? "active" : ""}`} onClick={() => setOnly("pending")}>
            Ждут решения{pending ? <span className="nav-badge">{pending}</span> : null}
          </button>
          <button className={`tab ${only === "issues" ? "active" : ""}`} onClick={() => setOnly("issues")}>Замечания</button>
        </div>
        <select className="feed-object" value={objectId} onChange={(e) => setObjectId(e.target.value)} aria-label="Объект">
          <option value="">Все объекты</option>
          {objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </div>

      {loading && <p className="muted">Загрузка…</p>}
      {!loading && events.length === 0 && (
        <div className="panel muted">
          {only === "pending" ? "Решений не ждёт ничего — ИИ справился сам." : only === "issues" ? "Замечаний нет."
            : "Пока ничего не пришло. Отправьте сообщение, фото или документ выше — или подключите Telegram-бот в настройках."}
        </div>
      )}
      {events.map((ev) => <EventCard key={ev.id} ev={ev} onChange={(e) => { replace(e); void load(); }} />)}
    </>
  );
}

// --- приём сигнала ------------------------------------------------------------------------------

function Composer({ objects, onSent }: { objects: { id: string; label: string }[]; onSent: (ev: IntakeEvent) => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [objectId, setObjectId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const [recording, setRecording] = useState(false);
  const [samples, setSamples] = useState<Sample[]>([]);
  const recorder = useRef<MediaRecorder | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (DEMO) api<Sample[]>("/ai/samples").then(setSamples).catch(() => setSamples([]));
  }, []);

  async function send(extra?: { sample?: string; voice?: Blob }) {
    if (busy) return;
    if (!extra && !file && !text.trim()) {
      setError("Напишите, что сделано, или приложите фото, голосовое, документ");
      return;
    }
    const form = new FormData();
    if (extra?.sample) form.append("sample", extra.sample);
    if (extra?.voice) form.append("file", extra.voice, "voice.webm");
    else if (file && !extra?.sample) form.append("file", file);
    if (text.trim() && !extra?.sample) form.append("text", text.trim());
    if (objectId) form.append("object_id", objectId);
    setBusy(true);
    setError("");
    try {
      const ev = await api<IntakeEvent>("/intake", { method: "POST", body: form });
      setText("");
      setFile(null);
      if (input.current) input.current.value = "";
      onSent(ev);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleVoice() {
    if (recording) {
      recorder.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        void send({ voice: new Blob(chunks, { type: rec.mimeType }) });
      };
      rec.start();
      recorder.current = rec;
      setRecording(true);
    } catch {
      setError("Нет доступа к микрофону");
    }
  }

  return (
    <div
      className={`panel composer-panel ${drag ? "drag" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files?.[0]; if (f) setFile(f); }}
    >
      <textarea
        value={text}
        placeholder="Что произошло на объекте? Например: «Залили бетон плиты Пм1 захватка 4 — 290 м3, бетона ушло 296 м3. Не привезли мембрану». Или перетащите сюда фото, УПД, сертификат, протокол, съёмку."
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send(); }}
      />
      <div className="composer-bar">
        <input ref={input} type="file" hidden accept="image/*,audio/*,.pdf,.docx,.xlsx,.txt,.csv,.eml"
               onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button className="btn" onClick={() => input.current?.click()} title="Фото, документ, съёмка">📎 Файл</button>
        <button className={`btn ${recording ? "recording" : ""}`} onClick={toggleVoice} title="Голосовое сообщение">
          {recording ? "■ Остановить" : "🎙 Голос"}
        </button>
        {file && <span className="file-chip">{file.name} <button className="link" onClick={() => setFile(null)}>×</button></span>}
        <select value={objectId} onChange={(e) => setObjectId(e.target.value)} className="composer-object" aria-label="Объект">
          <option value="">Объект определит ИИ</option>
          {objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
        <button className="btn primary" disabled={busy} onClick={() => void send()}>{busy ? "ИИ разбирает…" : "Отправить ИИ"}</button>
      </div>
      {samples.length > 0 && (
        <div className="samples">
          <span className="muted small">Примеры с площадки:</span>
          {samples.map((s) => (
            <button key={s.id} className="btn small" disabled={busy} title={s.hint} onClick={() => void send({ sample: s.id })}>{s.title}</button>
          ))}
        </div>
      )}
      {error && <div className="alert error" style={{ marginTop: 10, marginBottom: 0 }}>{error}</div>}
    </div>
  );
}

// --- карточка события ---------------------------------------------------------------------------

function EventCard({ ev, onChange }: { ev: IntakeEvent; onChange: (ev: IntakeEvent) => void }) {
  const [showText, setShowText] = useState(false);
  const proposed = ev.actions.filter((a) => a.status === "proposed" || a.status === "failed");
  const visible = ev.actions.filter((a) => a.kind !== "register_document");
  const registered = ev.actions.find((a) => a.kind === "register_document" && a.status === "applied");
  const issues = ev.actions.filter((a) => a.kind === "flag_issue" && a.severity !== "info").length;

  async function approveAll() {
    await api("/ai/actions/review-batch", { method: "POST", json: { ids: proposed.map((a) => a.id), approve: true } });
    onChange(await api<IntakeEvent>(`/ai/events/${ev.id}`));
  }

  return (
    <article className={`panel event ${proposed.length ? "has-pending" : ""} ${ev.status === "error" ? "is-error" : ""}`}>
      <header className="event-head">
        <span className="event-kind">{KIND_ICON[ev.kind]} {KIND[ev.kind]}</span>
        <span className="muted small">
          {ev.sender ? `${ev.sender} · ` : ""}{SOURCE[ev.source] ?? ev.source} · {time(ev.created_at)}
          {ev.object_name ? ` · ${ev.object_name.length > 48 ? ev.object_name.slice(0, 47) + "…" : ev.object_name}` : " · объект не определён"}
        </span>
        {issues > 0 && <span className="badge t-bad">{issues} замеч.</span>}
        {proposed.length > 0 && <span className="badge t-warn">ждёт решения: {proposed.length}</span>}
      </header>

      <div className="event-body">
        {ev.kind === "photo" && ev.file_url && <Thumb url={ev.file_url} />}
        <div className="event-input">
          {ev.text && <blockquote className="event-quote">{ev.text}</blockquote>}
          {ev.transcript && <blockquote className="event-quote">🎙 {ev.transcript}</blockquote>}
          {ev.file_name && ev.kind !== "photo" && (
            <div className="small">
              {ev.kind === "voice" ? "🎙" : "📄"} {ev.file_name}
              {ev.file_url && ev.kind === "voice" && <AudioPlayer url={ev.file_url} />}
              {registered?.result?.document_id && (
                <button className="link small" onClick={() => download(`/documents/${registered.result.document_id}/file`, ev.file_name ?? "file")}>открыть</button>
              )}
              {ev.excerpt && ev.kind === "document" && (
                <button className="link small" onClick={() => setShowText(!showText)}>{showText ? "скрыть текст" : "что прочитано"}</button>
              )}
            </div>
          )}
          {showText && ev.excerpt && <pre className="excerpt">{ev.excerpt}</pre>}
          {ev.needs_ocr && <div className="small text-warn">Скан без текстового слоя — для чтения нужна модель зрения или OCR.</div>}
          {ev.dispatcher && (
            <div className="small muted dispatcher">
              Диспетчер: <b>{ev.dispatcher.title}</b> · уверенность {pct(ev.dispatcher.confidence)} · {BY[ev.dispatcher.by] ?? ev.dispatcher.by}
              {registered && <> · в реестре и базе знаний</>}
            </div>
          )}
          {ev.status === "error" && <div className="alert error small">Сбой обработки: {ev.error}</div>}
        </div>
      </div>

      <div className="actions">
        {visible.map((a) => <ActionRow key={a.id} a={a} onDone={async () => onChange(await api<IntakeEvent>(`/ai/events/${ev.id}`))} />)}
      </div>

      {(proposed.length > 1 || (ev.source === "telegram" && ev.reply)) && (
        <footer className="event-foot">
          {ev.source === "telegram" && ev.reply && <details className="small muted"><summary>Ответ в Telegram</summary><pre className="excerpt">{ev.reply}</pre></details>}
          {proposed.length > 1 && <button className="btn small primary" onClick={() => void approveAll()}>Принять все ({proposed.length})</button>}
        </footer>
      )}
    </article>
  );
}

function Thumb({ url }: { url: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [big, setBig] = useState(false);
  useEffect(() => {
    let revoke = "";
    fetchBlob(apiPath(url)).then((b) => { revoke = URL.createObjectURL(b); setSrc(revoke); }).catch(() => setSrc(null));
    return () => { if (revoke) URL.revokeObjectURL(revoke); };
  }, [url]);
  if (!src) return <div className="thumb placeholder">📷</div>;
  return (
    <>
      <img className="thumb" src={src} alt="Фото с объекта" onClick={() => setBig(true)} />
      {big && <div className="modal-backdrop" onClick={() => setBig(false)}><img className="photo-big" src={src} alt="Фото с объекта" /></div>}
    </>
  );
}

function AudioPlayer({ url }: { url: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let revoke = "";
    fetchBlob(apiPath(url)).then((b) => { revoke = URL.createObjectURL(b); setSrc(revoke); }).catch(() => setSrc(null));
    return () => { if (revoke) URL.revokeObjectURL(revoke); };
  }, [url]);
  return src ? <audio controls src={src} className="audio" /> : null;
}

// --- действие ИИ --------------------------------------------------------------------------------

const STATUS_VIEW: Record<string, [string, string]> = {
  applied: ["✓", "ok"], proposed: ["?", "warn"], rejected: ["✗", "muted"], failed: ["!", "bad"], new: ["·", "muted"],
};

function ActionRow({ a, onDone }: { a: AIAction; onDone: () => Promise<void> }) {
  const [mode, setMode] = useState<"" | "edit" | "reject">("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const isIssue = a.kind === "flag_issue" && a.severity !== "info";
  const [icon, tone] = isIssue ? ["⚠", a.severity === "critical" ? "bad" : "warn"] : STATUS_VIEW[a.status] ?? ["·", "muted"];
  const reviewed = !!a.reviewed_at;

  async function review(approve: boolean, changes?: Record<string, unknown>) {
    setBusy(true);
    setError("");
    try {
      await api(`/ai/actions/${a.id}/review`, { method: "POST", json: { approve, reason: reason || null, changes: changes ?? null } });
      setMode("");
      await onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const statusText = a.status === "applied" ? (reviewed ? (a.corrected ? "исправлено и принято" : "принято") : a.auto ? "сделано ИИ" : "выполнено")
    : a.status === "proposed" ? "ждёт решения" : a.status === "rejected" ? (a.auto ? "отменено" : "отклонено") : a.status === "failed" ? "не выполнено" : "";
  const docId = a.result?.document_id as string | undefined;

  return (
    <div className={`action s-${a.status} ${isIssue ? `issue ${a.severity}` : ""}`}>
      <div className={`action-icon t-${tone}`}>{icon}</div>
      <div className="action-main">
        <div className="action-title">
          <span className="spec-name">{a.specialist_title}</span>
          <span>{a.title}</span>
        </div>
        {a.summary && <div className="small action-summary">{a.summary}</div>}
        {a.status === "failed" && a.result?.error && <div className="small text-bad">Причина: {String(a.result.error)}</div>}
        {a.status === "rejected" && a.reason && <div className="small muted">Причина: {a.reason}</div>}
        <div className="action-meta small muted">
          <span className={`status-${tone}`}>{statusText}</span>
          {a.kind !== "info" && !isIssue && <span> · уверенность {pct(a.confidence)}</span>}
          {a.result?.assignee && <span> · исполнитель: {String(a.result.assignee)}</span>}
          {a.evidence.filter((e) => e.type !== "file" && e.type !== "message" && e.label).slice(0, 3).map((e, i) => (
            <span key={i} className="evidence">{e.label}</span>
          ))}
          {docId && a.kind !== "register_document" && (
            <button className="link small" onClick={() => download(`/documents/${docId}/file`, a.title)}>открыть документ</button>
          )}
        </div>
        {error && <div className="small text-bad">{error}</div>}
        {mode === "edit" && <EditForm a={a} busy={busy} onSubmit={(changes) => review(true, changes)} onCancel={() => setMode("")} />}
        {mode === "reject" && (
          <div className="row reject-form">
            <input autoFocus value={reason} onChange={(e) => setReason(e.target.value)}
                   placeholder={isIssue ? "Почему это не проблема? ИИ учтёт" : a.status === "applied" ? "Что не так? ИИ отменит и учтёт" : "Почему? ИИ учтёт в следующий раз"} />
            <button className="btn small danger" disabled={busy} onClick={() => review(false)}>
              {isIssue ? "Это не проблема" : a.status === "applied" ? "Отменить действие" : "Отклонить"}
            </button>
            <button className="btn small" onClick={() => setMode("")}>Назад</button>
          </div>
        )}
      </div>
      {!mode && (
        <div className="action-buttons">
          {(a.status === "proposed" || a.status === "failed") && (
            <>
              <button className="btn small primary" disabled={busy} onClick={() => review(true)}>Принять</button>
              {a.editable.length > 0 && <button className="btn small" onClick={() => setMode("edit")}>Исправить</button>}
              <button className="btn small" onClick={() => setMode("reject")}>Отклонить</button>
            </>
          )}
          {a.status === "applied" && !reviewed && a.auto && a.kind !== "info" && (
            <>
              {a.editable.length > 0 && <button className="btn small" onClick={() => setMode("edit")}>Исправить</button>}
              <button className="btn small" onClick={() => setMode("reject")} title={isIssue ? "Ложная тревога — ИИ учтёт" : "Откатить сделанное ИИ"}>
                {isIssue ? "Неверно" : "Отменить"}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function EditForm({ a, busy, onSubmit, onCancel }: {
  a: AIAction; busy: boolean; onSubmit: (changes: Record<string, unknown>) => void; onCancel: () => void;
}) {
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    Object.fromEntries(a.editable.map((k) => [k, LIST_FIELDS.includes(k) ? ((a.payload[k] as string[] | undefined) ?? []).join("\n") : a.payload[k] ?? ""])));
  const materials = useOptions("/materials?limit=500", (r) => `${r.name} (${r.unit})`);
  const items = useOptions("/estimate-items?limit=500", (r) => `${r.code ? `${r.code} ` : ""}${r.name} (${r.unit})`);
  const objects = useObjects();
  const set = (k: string, v: unknown) => setValues((x) => ({ ...x, [k]: v }));

  function submit() {
    const changes: Record<string, unknown> = {};
    for (const k of a.editable) {
      let v = values[k];
      if (LIST_FIELDS.includes(k)) v = String(v ?? "").split("\n").map((s) => s.trim()).filter(Boolean);
      if (["quantity", "price", "due_days"].includes(k)) v = v === "" || v === null ? null : Number(v);
      const before = LIST_FIELDS.includes(k) ? (a.payload[k] ?? []) : a.payload[k] ?? "";
      if (JSON.stringify(v) !== JSON.stringify(before) && !(v === "" && before === "")) changes[k] = v;
    }
    onSubmit(changes);
  }

  const field = (k: string) => {
    const v = values[k] as string | number | undefined;
    if (k === "receipt_ids") return null;
    let control;
    if (k === "material_id") {
      control = (
        <select value={String(v ?? "")} onChange={(e) => set(k, e.target.value)}>
          <option value="">{a.payload.new_material ? `Новый: ${a.payload.new_material.name}` : "—"}</option>
          {materials.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      );
    } else if (k === "estimate_item_id") {
      control = (
        <select value={String(v ?? "")} onChange={(e) => set(k, e.target.value)}>
          {items.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      );
    } else if (k === "object_id") {
      control = (
        <select value={String(v ?? "")} onChange={(e) => set(k, e.target.value)}>
          <option value="">—</option>
          {objects.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      );
    } else if (k === "role") {
      control = (
        <select value={String(v ?? "")} onChange={(e) => set(k, e.target.value)}>
          {ROLE_OPTIONS.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      );
    } else if (k === "priority") {
      control = (
        <select value={String(v ?? "normal")} onChange={(e) => set(k, e.target.value)}>
          <option value="normal">Обычный</option><option value="high">Высокий</option><option value="low">Низкий</option>
        </select>
      );
    } else if (LIST_FIELDS.includes(k)) {
      control = <textarea rows={3} value={String(v ?? "")} onChange={(e) => set(k, e.target.value)} />;
    } else {
      const type = ["quantity", "price", "due_days"].includes(k) ? "number" : k === "date" ? "date" : "text";
      control = <input type={type} value={String(v ?? "")} onChange={(e) => set(k, e.target.value)} />;
    }
    return (
      <div className="field" key={k}>
        <label>{FIELD_LABELS[k] ?? k}</label>
        {control}
      </div>
    );
  };

  return (
    <div className="edit-form">
      <div className="edit-grid">{a.editable.map(field)}</div>
      <p className="small muted">Исправление станет примером: в следующий раз ИИ сопоставит так же.</p>
      <div className="row">
        <button className="btn small primary" disabled={busy} onClick={submit}>Сохранить и принять</button>
        <button className="btn small" onClick={onCancel}>Отмена</button>
      </div>
    </div>
  );
}

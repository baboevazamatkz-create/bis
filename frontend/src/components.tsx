// Общие компоненты и хуки интерфейса.
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import Icon from "./components/Icon";
import { api, DEMO, type Issue } from "./api";

export interface Option {
  id: string;
  label: string;
}

export function useOptions(path: string, label: (r: Record<string, unknown>) => string) {
  const [options, setOptions] = useState<Option[]>([]);
  useEffect(() => {
    api<Record<string, unknown>[]>(path)
      .then((rows) => setOptions(rows.map((r) => ({ id: String(r.id), label: label(r) }))))
      .catch(() => setOptions([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  return options;
}

export const useObjects = () => useOptions("/objects", (r) => `${r.code ? `${r.code} — ` : ""}${r.name}`);
export const useContracts = () => useOptions("/contracts", (r) => `№ ${r.number}${r.subject ? ` — ${String(r.subject).slice(0, 60)}` : ""}`);

export function Picker({ label, value, onChange, options, empty }: {
  label: string; value: string; onChange: (v: string) => void; options: Option[]; empty?: string;
}) {
  useEffect(() => {
    if (!value && options[0] && !empty) onChange(options[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options]);
  return (
    <div className="field">
      <label>{label}</label>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {empty !== undefined && <option value="">{empty}</option>}
        {options.map((o) => (
          <option key={o.id} value={o.id}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

export function Issues({ items }: { items?: Issue[] }) {
  if (!items?.length) return null;
  return (
    <ul className="issues">
      {items.map((i, n) => (
        <li key={n} className={i.level}>{i.level === "error" ? "Ошибка" : "Внимание"}: {i.message}</li>
      ))}
    </ul>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: "bad" | "warn" | "ok" }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export const num = (v: unknown, digits = 2) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: digits });

export const date = (v: unknown) => (v ? new Date(String(v)).toLocaleDateString("ru-RU") : "—");

export function useAsync() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  async function run<T>(fn: () => Promise<T>, success?: string): Promise<T | undefined> {
    setBusy(true);
    setError("");
    setOk("");
    try {
      const r = await fn();
      if (success) setOk(success);
      return r;
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const Alerts = () => (
    <>
      {error && <div className="alert error">{error}</div>}
      {ok && <div className="alert ok">{ok}</div>}
    </>
  );
  return { busy, run, Alerts };
}

export const STATUS_RU: Record<string, string> = {
  open: "Открыто", overdue: "Просрочено", done: "Исполнено", cancelled: "Отменено", in_progress: "В работе",
  on_track: "По графику", behind: "Отставание", ahead: "Опережение", not_started: "Не начато", no_data: "Нет данных",
  economy: "Экономия", overspend: "Перерасход", ok: "В норме", no_actual: "Нет факта", no_norm: "Нет нормы",
  reported: "Заявлено", confirmed: "Подтверждено", closed: "Закрыто", mismatch: "Расхождение",
};

export const tone = (s: string) =>
  ["overdue", "behind", "overspend", "mismatch", "critical"].includes(s) ? "bad"
    : ["no_actual", "no_norm", "warning", "reported"].includes(s) ? "warn"
    : ["done", "on_track", "ok", "economy", "confirmed", "closed", "ahead"].includes(s) ? "ok" : "";

export function Badge({ status }: { status: string }) {
  return <span className={`badge t-${tone(status)}`}>{STATUS_RU[status] ?? status}</span>;
}

/** Кнопка с подтверждением на странице (без системных диалогов). */
export function ConfirmButton({ label, confirmLabel, onConfirm, className = "btn small", disabled = false }: {
  label: string; confirmLabel: string; onConfirm: () => void; className?: string; disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return armed ? (
    <button disabled={disabled} className={`${className} danger`} onClick={() => { setArmed(false); onConfirm(); }}>{confirmLabel}</button>
  ) : (
    <button disabled={disabled} className={className} onClick={() => setArmed(true)}>{label}</button>
  );
}

/** Всплывающая подсказка: значок «?», текст — по наведению или нажатию. */
export function Hint({ children, wide }: { children: ReactNode; wide?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <span ref={ref} className={`hint ${open ? "open" : ""}`} onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" className="hint-icon" aria-label="Подсказка" onClick={() => setOpen((v) => !v)}>?</button>
      {open && <span className={`hint-pop ${wide ? "wide" : ""}`} role="tooltip">{children}</span>}
    </span>
  );
}

/** Заголовок страницы: название, подсказка, действия справа. */
export function PageHead({ title, hint, children }: { title: ReactNode; hint?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <h1>{title}{hint && <Hint wide>{hint}</Hint>}</h1>
      {children && <div className="page-actions">{children}</div>}
    </div>
  );
}

/** Голосовой режим: включается нажатием, сам завершается после паузы в речи и отдаёт распознанный текст.
 *  Запись → распознавание на сервере (SpeechKit/Whisper); в демо — речь браузера. */
export function useVoice(onText: (text: string) => void, { pauseMs = 2200, idleMs = 8000 } = {}) {
  const [state, setState] = useState<"idle" | "rec" | "busy">("idle");
  const [level, setLevel] = useState(0);
  const [err, setErr] = useState("");
  const stopRef = useRef<() => void>(() => {});
  const cb = useRef(onText);
  cb.current = onText;
  useEffect(() => () => stopRef.current(), []);

  async function start() {
    setErr("");
    if (DEMO) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const w = window as any;
      const SR = w.SpeechRecognition || w.webkitSpeechRecognition;
      if (!SR) { setErr("Браузер не распознаёт речь — откройте в Chrome"); return; }
      const rec = new SR();
      rec.lang = "ru-RU";
      rec.continuous = true;
      rec.interimResults = true;
      let said = "";
      let timer = setTimeout(() => rec.stop(), idleMs);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      rec.onresult = (e: any) => {
        said = Array.from(e.results).map((r: any) => r[0].transcript).join(" "); // eslint-disable-line @typescript-eslint/no-explicit-any
        setLevel(1);
        clearTimeout(timer);
        timer = setTimeout(() => rec.stop(), pauseMs);
      };
      rec.onerror = (e: { error?: string }) => { if (e.error !== "no-speech") setErr("Нет доступа к микрофону"); };
      rec.onend = () => { clearTimeout(timer); setState("idle"); setLevel(0); if (said.trim()) cb.current(said.trim()); };
      rec.start();
      stopRef.current = () => rec.stop();
      setState("rec");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setErr("Нет доступа к микрофону — разрешите его в браузере");
      return;
    }
    const rec = new MediaRecorder(stream);
    const chunks: Blob[] = [];
    // тишина: громкость микрофона ниже порога
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    const began = Date.now();
    let heard = false;
    let lastLoud = began;
    const tick = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      const rms = Math.sqrt(buf.reduce((s, v) => s + v * v, 0) / buf.length);
      setLevel(Math.min(1, rms * 12));
      const now = Date.now();
      if (rms > 0.02) { heard = true; lastLoud = now; }
      if ((heard && now - lastLoud > pauseMs) || (!heard && now - began > idleMs)) stop();
    }, 100);
    let stopped = false;
    function stop() {
      if (stopped) return;
      stopped = true;
      clearInterval(tick);
      void ctx.close();
      if (rec.state !== "inactive") rec.stop();
    }
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      setLevel(0);
      if (!heard) { setState("idle"); return; }
      setState("busy");
      const form = new FormData();
      form.append("file", new Blob(chunks, { type: rec.mimeType }), "voice.webm");
      try {
        const r = await api<{ text: string }>("/voice/transcribe", { method: "POST", body: form, timeoutMs: 120_000 });
        if (r.text.trim()) cb.current(r.text.trim());
        else setErr("Речь не распознана — повторите");
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setState("idle");
      }
    };
    rec.start();
    stopRef.current = stop;
    setState("rec");
  }

  const toggle = () => (state === "rec" ? stopRef.current() : state === "idle" ? void start() : undefined);
  return { state, level, err, toggle };
}

/** Кнопка голосового ввода: нажали — говорите; после паузы сама останавливается и вставляет текст. */
export function VoiceButton({ onText, className = "btn", label }: { onText: (text: string) => void; className?: string; label?: string }) {
  const v = useVoice(onText);
  return (
    <span className="voice">
      <button type="button" className={`${className} voice-btn ${v.state}`} disabled={v.state === "busy"}
              aria-label={v.state === "rec" ? "Остановить запись" : "Сказать голосом"}
              title={v.state === "rec" ? "Слушаю — остановлюсь сам после паузы (или нажмите)" : "Сказать голосом"}
              style={{ "--lvl": v.level } as CSSProperties} onClick={v.toggle}>
        {v.state === "rec" ? <><span className="voice-dot" />Слушаю…</> : v.state === "busy" ? "Распознаю…" : <><Icon name="mic" size={17} />{label ? ` ${label}` : ""}</>}
      </button>
      {v.err && <span className="small text-bad">{v.err}</span>}
    </span>
  );
}

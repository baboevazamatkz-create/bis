// Общие компоненты и хуки интерфейса.
import { useEffect, useState, type ReactNode } from "react";
import { api, type Issue } from "./api";

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
export function ConfirmButton({ label, confirmLabel, onConfirm, className = "btn small" }: {
  label: string; confirmLabel: string; onConfirm: () => void; className?: string;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(t);
  }, [armed]);
  return armed ? (
    <button className={`${className} danger`} onClick={() => { setArmed(false); onConfirm(); }}>{confirmLabel}</button>
  ) : (
    <button className={className} onClick={() => setArmed(true)}>{label}</button>
  );
}

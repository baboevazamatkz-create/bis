// «ИИ-специалисты»: кто в чём силён, на какой модели работает, насколько точен — и что ему разрешено делать самому.
import { useEffect, useState } from "react";
import { api, type User } from "../api";

interface Stats { total?: number; auto?: number; approved?: number; corrected?: number; rejected?: number; pending?: number; failed?: number; accuracy?: number | null }
interface Specialist {
  name: string; title: string; strength: string; capability: string; agent: string | null; connected: boolean; model: string; stats: Stats;
}
interface Autonomy { kind: string; title: string; mode: "auto" | "propose"; default: string }
interface Suggestion { kind: string; title: string; suggest: "auto" | "propose"; decisions: number; reason: string }
interface Data { specialists: Specialist[]; autonomy: Autonomy[]; min_confidence: number; suggestions: Suggestion[] }
interface ModelCheck {
  provider: string; model: string; ok: boolean; reply?: string; json?: boolean | null; seconds?: number; routes: string[];
  error?: string; hint?: string | null;
}

const CAPABILITY: Record<string, string> = { text: "языковая модель", vision: "модель зрения", speech: "распознавание речи", rules: "алгоритмы" };

export default function Specialists({ user }: { user: User }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [checks, setChecks] = useState<ModelCheck[] | null>(null);
  const [checking, setChecking] = useState(false);
  const isAdmin = user.role === "admin";

  async function checkModels() {
    setChecking(true);
    setError("");
    try {
      setChecks((await api<{ providers: ModelCheck[] }>("/ai/models/check", { method: "POST" })).providers);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChecking(false);
    }
  }
  const load = () => api<Data>("/ai/specialists").then(setData).catch((e) => setError(e.message));
  useEffect(() => { void load(); }, []);

  async function setMode(kind: string, mode: "auto" | "propose") {
    setError("");
    try {
      await api("/ai/autonomy", { method: "PUT", json: { modes: { [kind]: mode } } });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (!data) return <div className="muted">{error || "Загрузка…"}</div>;
  const connected = data.specialists.filter((s) => s.connected).length;

  return (
    <>
      <h1>ИИ-специалисты</h1>
      <p className="muted feed-lead">
        Каждый специалист силён в своём и работает на своей модели. Без моделей часть работы выполняют алгоритмы и правила;
        подключите модели (локальные или облачные российские) — точность и охват вырастут. Подключено: {connected} из {data.specialists.length}.
      </p>
      {error && <div className="alert error">{error}</div>}
      {isAdmin && (
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <h2 style={{ margin: 0 }}>Подключение моделей</h2>
            <button className="btn small" disabled={checking} onClick={() => void checkModels()}>
              {checking ? "Проверяю…" : "Проверить модели"}
            </button>
          </div>
          {checks && checks.length === 0 && <p className="small muted">Модели не настроены — работают правила (офлайн-режим).</p>}
          {checks?.map((c) => (
            <div key={c.provider} className={`note ${c.ok ? "" : "critical"}`} style={{ marginTop: 8 }}>
              <b>{c.ok ? "✓" : "✗"} {c.provider}</b> <span className="small muted">{c.model}</span>
              {c.ok ? (
                <div className="small">Ответ: «{c.reply}» за {c.seconds} с · JSON: {c.json ? "да" : "нет"} · задачи: {c.routes.join(", ") || "—"}</div>
              ) : (
                <div className="small">{c.hint ?? c.error}</div>
              )}
            </div>
          ))}
        </div>
      )}

      {data.suggestions.length > 0 && (
        <div className="panel suggestions-panel">
          <h2>ИИ предлагает</h2>
          {data.suggestions.map((s) => (
            <div key={s.kind} className="row" style={{ alignItems: "center", marginBottom: 6 }}>
              <span style={{ flex: 1 }}><b>{s.title}</b>: {s.reason}.</span>
              {isAdmin && (
                <button className="btn small primary" onClick={() => void setMode(s.kind, s.suggest)}>
                  {s.suggest === "auto" ? "Пусть делает сам" : "Пусть спрашивает"}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="spec-grid">
        {data.specialists.map((s) => {
          const st = s.stats ?? {};
          return (
            <div key={s.name} className={`panel spec-card ${s.connected ? "" : "off"}`}>
              <div className="spec-head">
                <span className={`dot ${s.connected ? "on" : ""}`} />
                <b>{s.title}</b>
                <span className="muted small">{CAPABILITY[s.capability] ?? s.capability}</span>
              </div>
              <div className="small">{s.strength}</div>
              <div className="small muted spec-model">{s.model}</div>
              {st.total ? (
                <div className="spec-stats small">
                  <span title="Сделал сам">сам: <b>{st.auto ?? 0}</b></span>
                  <span title="Принято людьми без правок">принято: <b>{st.approved ?? 0}</b></span>
                  <span title="Принято после исправления">исправлено: <b>{st.corrected ?? 0}</b></span>
                  <span title="Отклонено или отменено">отклонено: <b>{st.rejected ?? 0}</b></span>
                  {!!st.pending && <span>ждёт: <b>{st.pending}</b></span>}
                  {st.accuracy !== null && st.accuracy !== undefined && <span className="accuracy">точность {st.accuracy}%</span>}
                </div>
              ) : <div className="spec-stats small muted">пока без работы</div>}
            </div>
          );
        })}
      </div>

      <div className="panel">
        <h2>Что ИИ делает сам</h2>
        <p className="small muted">
          «Сам» — действие выполняется сразу (при уверенности не ниже {Math.round(data.min_confidence * 100)}%) и видно в ленте,
          его можно отменить. «Спрашивать» — ИИ готовит действие и ждёт решения человека.
          {!isAdmin && " Менять может администратор."}
        </p>
        <table>
          <thead><tr><th>Действие</th><th>Режим</th><th className="muted">По умолчанию</th></tr></thead>
          <tbody>
            {data.autonomy.map((a) => (
              <tr key={a.kind}>
                <td>{a.title}</td>
                <td>
                  <div className="seg">
                    <button className={`seg-btn ${a.mode === "auto" ? "active" : ""}`} disabled={!isAdmin}
                            onClick={() => a.mode !== "auto" && void setMode(a.kind, "auto")}>Сам</button>
                    <button className={`seg-btn ${a.mode === "propose" ? "active" : ""}`} disabled={!isAdmin}
                            onClick={() => a.mode !== "propose" && void setMode(a.kind, "propose")}>Спрашивать</button>
                  </div>
                </td>
                <td className="muted small">{a.default === "auto" ? "сам" : "спрашивать"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

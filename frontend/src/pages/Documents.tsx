import { useEffect, useState } from "react";
import { api, download, upload, STATUS_LABELS, type DocumentRow } from "../api";
import { Hint } from "../components";

export default function Documents({ workspace = "platform", collection = "records", allowedKinds, title = "Документы", hideUpload = false }: {
  workspace?: "platform" | "executive"; collection?: "records" | "sources"; allowedKinds?: string[]; title?: string; hideUpload?: boolean;
}) {
  const [kinds, setKinds] = useState<Record<string, string>>({});
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [uploadKind, setUploadKind] = useState(allowedKinds?.[0] ?? "incoming_letter");
  const [uploadTitle, setUploadTitle] = useState("");

  const load = () => {
    const q = new URLSearchParams();
    q.set("workspace", workspace);
    q.set("collection", collection);
    if (kind) q.set("kind", kind);
    if (status) q.set("status", status);
    api<DocumentRow[]>(`/documents?${q}`).then((rows) => setDocs(allowedKinds ? rows.filter((d) => allowedKinds.includes(d.kind)) : rows)).catch((e) => setError(e.message));
  };
  useEffect(() => {
    api<Record<string, string>>("/documents/kinds").then(setKinds);
  }, []);
  useEffect(load, [kind, status, workspace, collection, allowedKinds]);

  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  async function act(d: DocumentRow, action: "approve" | "reject") {
    setError("");
    try {
      if (action === "reject") {
        if (!reason.trim()) return;
        await api(`/documents/${d.id}/reject`, { method: "POST", json: { reason } });
        setRejecting(null);
        setReason("");
      } else {
        await api(`/documents/${d.id}/approve`, { method: "POST" });
      }
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function register() {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("kind", uploadKind);
    if (uploadTitle) form.append("title", uploadTitle);
    try {
      await upload("/documents/upload", form);
      setFile(null);
      setUploadTitle("");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <>
      <h1>{title}</h1>
      {error && <div className="alert error">{error}</div>}
      <div className="panel">
        <div className="row">
          <div className="field">
            <label>Вид</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">Все</option>
              {Object.entries(kinds).filter(([k]) => !allowedKinds || allowedKinds.includes(k)).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Статус</label>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Все</option>
              {["draft", "approved", "rejected", "registered"].map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Документ</th>
              <th>Статус</th>
              <th>Создал</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {docs.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  Документов пока нет
                </td>
              </tr>
            )}
            {docs.map((d) => (
              <tr key={d.id}>
                <td>
                  <div>{d.title}</div>
                  <div className="muted small">
                    {kinds[d.kind] ?? d.kind} · {new Date(d.created_at).toLocaleString("ru-RU")}
                  </div>
                  {d.issues?.length > 0 && (
                    <ul className="issues">
                      {d.issues.map((i, n) => (
                        <li key={n} className={i.level}>
                          {i.level === "error" ? "Ошибка" : "Внимание"}: {i.message}
                        </li>
                      ))}
                    </ul>
                  )}
                  {d.reject_reason && <div className="small issues error">Отклонён: {d.reject_reason}</div>}
                </td>
                <td>
                  <span className={`badge ${d.status}`}>{STATUS_LABELS[d.status] ?? d.status}</span>
                </td>
                <td className="small muted">{d.created_by?.startsWith("agent:") ? `агент ${d.created_by.slice(6)}` : "пользователь"}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {d.file_name && (
                    <button className="btn small" onClick={() => download(`/documents/${d.id}/file`, d.file_name ?? undefined)}>
                      Скачать
                    </button>
                  )}{" "}
                  {["draft", "rejected"].includes(d.status) && (
                    <button className="btn small primary" onClick={() => act(d, "approve")}>
                      Утвердить
                    </button>
                  )}{" "}
                  {d.status === "draft" && rejecting !== d.id && (
                    <button className="btn small" onClick={() => { setRejecting(d.id); setReason(""); }}>
                      Отклонить
                    </button>
                  )}
                  {rejecting === d.id && (
                    <div className="row" style={{ marginTop: 6 }}>
                      <input id={`reason-${d.id}`} value={reason} autoFocus placeholder="Причина (станет правилом для агента)"
                        onChange={(e) => setReason(e.target.value)} onKeyDown={(e) => e.key === "Enter" && act(d, "reject")} />
                      <button className="btn small primary" disabled={!reason.trim()} onClick={() => act(d, "reject")}>Отклонить</button>
                      <button className="btn small" onClick={() => setRejecting(null)}>Отмена</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!hideUpload && <div className="panel">
        <h2>Регистрация входящего документа <Hint>Файл попадёт в реестр и в базу знаний организации — агенты смогут на него ссылаться.</Hint></h2>
        <div className="row">
          <div className="field">
            <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </div>
          <div className="field">
            <select value={uploadKind} onChange={(e) => setUploadKind(e.target.value)}>
              {(allowedKinds ?? ["incoming_letter", "contract", "order", "other"]).map((k) => (
                <option key={k} value={k}>
                  {kinds[k] ?? k}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <input placeholder="Название" value={uploadTitle} onChange={(e) => setUploadTitle(e.target.value)} />
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!file} onClick={register}>
              Зарегистрировать
            </button>
          </div>
        </div>
      </div>}
    </>
  );
}

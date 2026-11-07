import { useEffect, useState } from "react";
import { api, upload, STATUS_LABELS, type Citation, type Source, type User } from "../api";
import { ConfirmButton, VoiceButton } from "../components";

export default function Knowledge({ user }: { user: User }) {
  const [kinds, setKinds] = useState<Record<string, string>>({});
  const [sources, setSources] = useState<Source[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState("norm");
  const [title, setTitle] = useState("");
  const [designation, setDesignation] = useState("");
  const [scope, setScope] = useState("organization");
  const [query, setQuery] = useState("");
  const [searchScope, setSearchScope] = useState("all");
  const [results, setResults] = useState<Citation[] | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api<Source[]>("/knowledge/sources").then(setSources);
  useEffect(() => {
    api<Record<string, string>>("/knowledge/kinds").then(setKinds);
    load();
  }, []);

  async function send() {
    if (!file) return;
    setBusy(true);
    setMsg(null);
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    form.append("scope", scope);
    if (title) form.append("title", title);
    if (designation) form.append("designation", designation);
    try {
      const s = await upload<Source>("/knowledge/upload", form);
      const ocr = s.meta?.needs_ocr ? " Похоже на скан без текста — нужен OCR (фаза 2)." : "";
      setMsg({ ok: true, text: `Загружено: ${s.designation ?? s.title}, фрагментов: ${s.chunk_count}.${ocr}` });
      setFile(null);
      setTitle("");
      setDesignation("");
      load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function search(q: string = query) {
    if (!q.trim()) return;
    const r = await api<{ results: Citation[] }>(`/knowledge/search?q=${encodeURIComponent(q)}&scope=${searchScope}`);
    setResults(r.results);
  }

  async function setStatus(s: Source, status: string) {
    try {
      await api(`/knowledge/sources/${s.id}`, { method: "PATCH", json: { status } });
      load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  }

  async function remove(s: Source) {
    try {
      await api(`/knowledge/sources/${s.id}`, { method: "DELETE" });
      load();
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    }
  }

  return (
    <>
      <h1>База знаний</h1>
      <div className="panel">
        <h2>Поиск</h2>
        <div className="row">
          <div className="field">
            <input
              value={query}
              placeholder="Спросите своими словами: «через сколько снимать опалубку?» или «СП 70.13330.2012 п. 5.16»"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void search()}
            />
          </div>
          <div className="field" style={{ flex: "0 0 180px" }}>
            <select value={searchScope} onChange={(e) => setSearchScope(e.target.value)}>
              <option value="all">Везде</option>
              <option value="norms">Нормативы</option>
              <option value="organization">Документы компании</option>
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <div className="row" style={{ gap: 6, alignItems: "flex-start" }}>
              <VoiceButton onText={(t) => { setQuery(t); void search(t); }} />
              <button className="btn primary" onClick={() => void search()}>
                Найти
              </button>
            </div>
          </div>
        </div>
        {results && results.length === 0 && <p className="muted">Ничего не найдено.</p>}
        {results?.map((r, i) => (
          <details key={r.chunk_id} open={i < 3} style={{ marginBottom: 6 }}>
            <summary>
              <b>{r.citation}</b> <span className="muted small">{r.section_path}</span>
            </summary>
            <div className="small" style={{ whiteSpace: "pre-wrap", padding: "6px 0" }}>
              {r.content}
            </div>
          </details>
        ))}
      </div>

      <div className="panel">
        <h2>Загрузка документов</h2>
        {msg && <div className={`alert ${msg.ok ? "ok" : "error"}`}>{msg.text}</div>}
        <div className="row">
          <div className="field">
            <label>Файл (PDF, DOCX, XLSX, TXT, EML)</label>
            <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </div>
          <div className="field">
            <label>Тип</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {Object.entries(kinds).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </div>
          {user.is_platform_admin && (
            <div className="field">
              <label>Доступ</label>
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="organization">Только организация</option>
                <option value="global">Общая библиотека платформы</option>
              </select>
            </div>
          )}
        </div>
        <div className="row">
          <div className="field">
            <label>Название (необязательно)</label>
            <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="field">
            <label>Обозначение (определяется автоматически)</label>
            <input value={designation} placeholder="СП 70.13330.2012" onChange={(e) => setDesignation(e.target.value)} />
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!file || busy} onClick={send}>
              {busy ? "Обработка…" : "Загрузить"}
            </button>
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>Источники ({sources.length})</h2>
        <table>
          <thead>
            <tr>
              <th>Документ</th>
              <th>Тип</th>
              <th>Статус</th>
              <th>Фрагментов</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id}>
                <td>
                  <b>{s.designation ?? ""}</b> {s.title}{" "}
                  {s.is_global && <span className="badge global">общая библиотека</span>}
                </td>
                <td className="small">{kinds[s.kind] ?? s.kind}</td>
                <td>
                  <select value={s.status} onChange={(e) => setStatus(s, e.target.value)} style={{ width: 140 }}>
                    {["active", "cancelled", "replaced", "unknown"].map((st) => (
                      <option key={st} value={st}>
                        {STATUS_LABELS[st]}
                      </option>
                    ))}
                  </select>
                </td>
                <td>{s.chunk_count}</td>
                <td>
                  <ConfirmButton label="Удалить" confirmLabel="Точно удалить?" onConfirm={() => remove(s)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

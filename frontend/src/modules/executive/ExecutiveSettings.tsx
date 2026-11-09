import { useEffect, useState } from "react";
import { api, upload } from "../../api";
import { PageHead } from "../../components";

interface Org { name: string; short_name: string | null; inn: string | null; kpp: string | null; ogrn: string | null; address: string | null; sro: string | null }
interface Template { id: string; name: string; kind: string; is_default: boolean }
const fields: [keyof Org, string][] = [["name", "Организация"], ["short_name", "Краткое наименование"], ["inn", "ИНН"], ["kpp", "КПП"], ["ogrn", "ОГРН"], ["address", "Адрес"], ["sro", "Сведения о СРО"]];
export default function ExecutiveSettings() {
  const [org, setOrg] = useState<Org | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const loadTemplates = () => api<Template[]>("/templates").then((list) => setTemplates(list.filter((t) => t.kind === "aosr")));
  useEffect(() => { api<Org>("/org").then(setOrg).catch((e) => setError(e.message)); void loadTemplates().catch((e) => setError(e.message)); }, []);
  async function save() {
    if (!org) return;
    setBusy(true); setError(""); setMessage("");
    try { await api("/org", { method: "PATCH", json: Object.fromEntries(fields.map(([k]) => [k, org[k]])) }); setMessage("Реквизиты сохранены"); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function addTemplate() {
    if (!file) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const form = new FormData(); form.append("file", file); form.append("kind", "aosr"); form.append("name", file.name);
      await upload("/templates", form); await loadTemplates(); setFile(null); setMessage("Шаблон АОСР загружен");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <><PageHead title="Настройки исполнительной документации" hint="Реквизиты строительной организации и шаблоны актов. Участники и назначения настраиваются в карточках объектов." />
    {error && <div className="alert error">{error}</div>}{message && <div className="alert ok">{message}</div>}
    <div className="panel"><h2>Реквизиты для актов</h2>{!org ? <p className="muted">Загрузка…</p> : <><div className="row">{fields.map(([key, label]) => <div className="field" key={key}><label htmlFor={`id-org-${key}`}>{label}</label><input id={`id-org-${key}`} value={org[key] ?? ""} onChange={(e) => setOrg({ ...org, [key]: e.target.value })} /></div>)}</div><button className="btn primary" disabled={busy} onClick={() => void save()}>Сохранить реквизиты</button></>}</div>
    <div className="panel"><h2>Шаблоны АОСР</h2><p className="muted">DOCX с полями шаблона BIS, например {"{{ number }}"}, {"{{ date_text }}"}, {"{{ developer_text }}"}.</p><div className="row"><div className="field"><label htmlFor="aosr-template">Файл шаблона</label><input id="aosr-template" type="file" accept=".docx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></div><button className="btn primary" disabled={!file || busy} onClick={() => void addTemplate()}>Загрузить шаблон</button></div>
      {templates.length ? <ul>{templates.map((t) => <li key={t.id}>{t.name}{t.is_default && " · по умолчанию"}</li>)}</ul> : <p className="muted">Используется встроенная форма АОСР.</p>}
    </div></>;
}

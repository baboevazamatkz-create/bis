// «Ось» — мозг объекта: что BIS знает о документах, как они связаны, что прочитано зрением.
import { useEffect, useState } from "react";
import { api, openFile } from "../api";
import { Hint, PageHead, VoiceButton } from "../components";

interface Coverage {
  documents: number; cards: number; cards_by_model: number; vision: number; links: number; links_found: number;
  chunks_with_page: number; pending: number; vision_waiting: number; vision_available: boolean;
}
interface Found { id: string; title: string; kind: string; date: string | null; card: string | null; excerpt: string | null }
interface LinkRow { relation: string; label: string; number: string; found?: boolean; document: { id: string; title: string } | null }
interface Sheet {
  sheet_type: string; description: string; title_block: Record<string, string | null>; deviations: { point: string; plan_mm: string | null; height_mm: string | null }[];
}
interface CardView {
  id: string; title: string; kind: string; file_name: string | null; date: string | null; text_from: string | null;
  card: Record<string, unknown> & { vision?: { sheets?: Sheet[]; error?: string } | null } | null;
  references: LinkRow[]; referenced_by: LinkRow[];
}

const FIELDS: [string, string][] = [
  ["number", "Номер"], ["rd_code", "Шифр РД"], ["sheets", "Листы"], ["revision", "Изменение"], ["elements", "Конструкции"],
  ["works", "Работы"], ["grades", "Марки и классы"], ["axes", "Оси"], ["levels", "Отметки"], ["norms", "Нормы"],
  ["sender", "Отправитель"], ["recipient", "Получатель"], ["work_start", "Начало работ"], ["work_end", "Окончание работ"],
  ["deadline", "Срок"],
];
const ruDate = (v: unknown) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? new Date(v).toLocaleDateString("ru-RU") : v);
const show = (v: unknown): string =>
  Array.isArray(v) ? v.map((x) => (typeof x === "object" && x ? Object.values(x).filter(Boolean).join(" ") : String(x))).join(", ")
    : String(ruDate(v) ?? "");

export default function Brain() {
  const [cov, setCov] = useState<Coverage | null>(null);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<Found[] | null>(null);
  const [view, setView] = useState<CardView | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const load = () => api<Coverage>("/brain/coverage").then(setCov).catch(() => setCov(null));
  useEffect(() => { void load(); }, []);

  async function search(text = q) {
    setView(null);
    setFound(await api<Found[]>(`/brain/search?q=${encodeURIComponent(text)}`));
  }
  async function open(id: string) {
    setView(await api<CardView>(`/brain/documents/${id}`));
  }
  async function buildNow() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api<{ cards: number; vision?: number | string; left: number }>("/brain/build", { method: "POST" });
      setMsg(`Карточек построено: ${r.cards}` + (typeof r.vision === "number" ? `, листов прочитано зрением: ${r.vision}` : ""));
      await load();
    } finally {
      setBusy(false);
    }
  }

  const card = view?.card;
  const sheets = card?.vision?.sheets ?? [];

  return (
    <>
      <PageHead title="Ось" hint={<>Мозг объекта: для каждого документа — карточка (вид, номер, дата, РД, конструкции, марки,
        оси, отметки, суть), связи с другими документами и описание листов, прочитанных зрением. На «Оси» держатся ответы
        ассистента и Telegram-бота.</>}>
        <button className="btn" disabled={busy} onClick={() => void buildNow()}>{busy ? "Досчитываю…" : "Досчитать сейчас"}</button>
      </PageHead>
      {msg && <div className="alert ok">{msg}</div>}
      {cov && (
        <div className="stats">
          <div className="stat"><div className="stat-value">{cov.cards}<span className="muted small"> / {cov.documents}</span></div><div className="stat-label">карточек</div></div>
          <div className="stat"><div className="stat-value">{cov.links_found}<span className="muted small"> / {cov.links}</span></div><div className="stat-label">связей найдено</div></div>
          <div className="stat"><div className="stat-value">{cov.chunks_with_page}</div><div className="stat-label">фрагментов со страницей</div></div>
          <div className={`stat ${cov.vision_available ? "" : "warn"}`}>
            <div className="stat-value">{cov.vision}{cov.vision_waiting ? <span className="muted small"> +{cov.vision_waiting} ждут</span> : null}</div>
            <div className="stat-label">листов прочитано зрением{cov.vision_available ? "" : " (зрение не подключено)"}</div>
          </div>
          {cov.pending > 0 && <div className="stat warn"><div className="stat-value">{cov.pending}</div><div className="stat-label">ждут карточки</div></div>}
        </div>
      )}

      <div className="panel">
        <div className="row" style={{ alignItems: "center" }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void search()}
                 placeholder="Что найти: «А500С 1-6/А-Г», «схема плиты Фм1», «письмо заказчика про ИД», «КЖ0 лист 4»" style={{ flex: 1 }} />
          <VoiceButton onText={(t) => { setQ(t); void search(t); }} />
          <button className="btn primary" onClick={() => void search()}>Найти</button>
        </div>
        {found && found.length === 0 && <p className="muted">Ничего не нашлось.</p>}
        {found && found.length > 0 && !view && (
          <div className="brain-list">
            {found.map((d) => (
              <button key={d.id} className="brain-item" onClick={() => void open(d.id)}>
                <b>{d.card || d.title}</b>
                {d.excerpt && <span className="small muted">{d.excerpt.slice(0, 220)}…</span>}
              </button>
            ))}
          </div>
        )}
      </div>

      {view && (
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <h2 style={{ margin: 0 }}>{(card?.title as string) || view.title}</h2>
            <div className="row">
              <button className="btn small" onClick={() => void openFile(`/documents/${view.id}/file`, view.title)}>Открыть</button>
              <button className="btn small" onClick={() => setView(null)}>← К списку</button>
            </div>
          </div>
          {card?.summary ? <p>{String(card.summary)}</p> : null}
          {!card && <p className="muted">Карточка ещё строится — нажмите «Досчитать сейчас».</p>}
          {card && (
            <table className="brain-card">
              <tbody>
                {FIELDS.filter(([k]) => card[k] && (!Array.isArray(card[k]) || (card[k] as unknown[]).length)).map(([k, label]) => (
                  <tr key={k}><td className="muted">{label}</td><td>{show(card[k])}</td></tr>
                ))}
                {Array.isArray(card.materials) && card.materials.length > 0 && (
                  <tr><td className="muted">Материалы</td><td>{show(card.materials)}</td></tr>
                )}
              </tbody>
            </table>
          )}
          {view.references.length > 0 && (
            <>
              <h3>Ссылается на <Hint>Документы, номера которых указаны в тексте. ✗ — в архиве такого документа нет.</Hint></h3>
              <ul className="brain-links">
                {view.references.map((r, i) => (
                  <li key={i} className={r.found ? "" : "text-bad"}>
                    {r.found ? "✓" : "✗"} {r.label} № {r.number}
                    {r.document && <> — <button className="link" onClick={() => void open(r.document!.id)}>{r.document.title}</button></>}
                    {!r.found && " — нет в архиве"}
                  </li>
                ))}
              </ul>
            </>
          )}
          {view.referenced_by.length > 0 && (
            <>
              <h3>На него ссылаются</h3>
              <ul className="brain-links">
                {view.referenced_by.map((r, i) => r.document && (
                  <li key={i}><button className="link" onClick={() => void open(r.document!.id)}>{r.document.title}</button> <span className="muted small">({r.label})</span></li>
                ))}
              </ul>
            </>
          )}
          {sheets.length > 0 && (
            <>
              <h3>Листы — что увидело зрение</h3>
              {sheets.map((s, i) => (
                <div key={i} className="brain-sheet">
                  <b>Лист {s.title_block?.sheet || i + 1}{s.title_block?.title ? ` «${s.title_block.title}»` : ""}</b>
                  <span className="muted small"> · {s.sheet_type}{s.title_block?.code ? ` · ${s.title_block.code}` : ""}{s.title_block?.revision ? ` · изм. ${s.title_block.revision}` : ""}</span>
                  <div>{s.description}</div>
                  {s.deviations?.length > 0 && (
                    <div className="small">Отклонения: {s.deviations.map((d) => `${d.point}: ${d.plan_mm ?? "—"} / ${d.height_mm ?? "—"} мм`).join("; ")}</div>
                  )}
                </div>
              ))}
            </>
          )}
          {card?.vision?.error && <p className="small text-bad">Зрение не прочитало листы: {card.vision.error}</p>}
          {view.text_from === "none" && !sheets.length && (
            <p className="small muted">В файле нет текста — его прочитает зрение, когда модель будет подключена.</p>
          )}
        </div>
      )}
    </>
  );
}

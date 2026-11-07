import { useEffect, useState } from "react";
import { api, type DocumentRow, type Issue } from "../api";
import { PageHead } from "../components";

const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

export default function AosrForm({ onCreated }: { onCreated: () => void }) {
  const [objects, setObjects] = useState<{ id: string; name: string; code: string | null }[]>([]);
  const [f, setF] = useState({
    object_id: "", works: "", project_docs: "", materials: "", executive_docs: "", norms: "",
    work_start: "", work_end: "", act_date: new Date().toISOString().slice(0, 10), next_works: "", additional_info: "",
  });
  const [issues, setIssues] = useState<Issue[] | null>(null);
  const [reps, setReps] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ id: string; name: string; code: string | null }[]>("/objects").then((o) => {
      setObjects(o);
      if (o[0]) setF((x) => ({ ...x, object_id: o[0].id }));
    });
  }, []);

  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const body = () => ({
    ...f,
    materials: lines(f.materials),
    executive_docs: lines(f.executive_docs),
    norms: lines(f.norms),
    next_works: f.next_works || null,
    additional_info: f.additional_info || null,
    act_date: f.act_date || null,
  });

  async function check() {
    setError("");
    try {
      const r = await api<{ issues: Issue[]; representatives?: Record<string, string> }>("/documents/aosr/check", {
        method: "POST",
        json: body(),
      });
      setIssues(r.issues);
      setReps(r.representatives ?? {});
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function create() {
    setBusy(true);
    setError("");
    try {
      await api<DocumentRow>("/documents/aosr", { method: "POST", json: body() });
      onCreated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const area = (k: keyof typeof f, label: string, hint?: string) => (
    <div className="field">
      <label>{label}</label>
      <textarea value={f[k]} placeholder={hint} onChange={set(k)} />
    </div>
  );

  return (
    <>
      <PageHead title="Акт освидетельствования скрытых работ" hint={<>Участники и представители подставляются из карточки объекта и приказов о назначении (раздел «Назначения»). Нумерация —
        автоматическая по объекту. Тот же акт можно попросить у агента «ПТО» в чате.</>} />
      {error && <div className="alert error">{error}</div>}
      <div className="panel">
        <div className="row">
          <div className="field">
            <label>Объект</label>
            <select value={f.object_id} onChange={set("object_id")}>
              {objects.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.code ? `${o.code} — ` : ""}
                  {o.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 170px" }}>
            <label>Дата акта</label>
            <input type="date" value={f.act_date} onChange={set("act_date")} />
          </div>
        </div>
        {area("works", "1. Работы, предъявленные к освидетельствованию", "Армирование фундаментной плиты Пм1 в осях 1-5/А-Г")}
        {area("project_docs", "2. Проектная документация", "ЖК-01-КЖ1, листы 3-7, ООО «ПроектДемо»")}
        {area("materials", "3. Материалы — по строке, со ссылкой на документ о качестве", "Арматура А500С d12, сертификат № 123 от 01.05.2026")}
        {area("executive_docs", "4. Исполнительные схемы, протоколы — по строке", "Исполнительная схема № 5")}
        <div className="row">
          <div className="field">
            <label>5. Начало работ</label>
            <input type="date" value={f.work_start} onChange={set("work_start")} />
          </div>
          <div className="field">
            <label>Окончание работ</label>
            <input type="date" value={f.work_end} onChange={set("work_end")} />
          </div>
        </div>
        {area("norms", "6. Нормы и разделы ПД — по строке", "СП 70.13330.2012, п. 5.16")}
        <div className="field">
          <label>7. Разрешается производство последующих работ</label>
          <input value={f.next_works} onChange={set("next_works")} />
        </div>
        <div className="field">
          <label>Дополнительные сведения</label>
          <input value={f.additional_info} onChange={set("additional_info")} />
        </div>
        <div className="row">
          <button className="btn" onClick={check}>
            Проверить
          </button>
          <button className="btn primary" disabled={busy || !f.works || !f.work_start || !f.work_end} onClick={create}>
            Сформировать черновик
          </button>
        </div>
      </div>
      {issues && (
        <div className="panel">
          <h2>Проверка</h2>
          {issues.length === 0 ? (
            <div className="alert ok">Замечаний нет</div>
          ) : (
            <ul className="issues">
              {issues.map((i, n) => (
                <li key={n} className={i.level}>
                  {i.level === "error" ? "Ошибка" : "Внимание"}: {i.message}
                </li>
              ))}
            </ul>
          )}
          {Object.keys(reps).length > 0 && (
            <>
              <h2 style={{ marginTop: 12 }}>Представители по приказам</h2>
              <ul className="small">
                {Object.entries(reps).map(([k, v]) => (
                  <li key={k}>{v}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </>
  );
}

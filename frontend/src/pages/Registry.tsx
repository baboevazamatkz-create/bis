import { useEffect, useState } from "react";
import { api } from "../api";

interface FieldDef {
  key: string;
  label: string;
  type?: "text" | "date" | "number" | "select" | "ref" | "textarea";
  options?: Record<string, string>;
  ref?: string; // ключ реестра
  required?: boolean;
  list?: boolean; // показывать в таблице
}

interface RegistryDef {
  title: string;
  path: string;
  labelKey: string;
  fields: FieldDef[];
}

const ROLES: Record<string, string> = {
  developer_control: "Застройщик/техзаказчик — строительный контроль",
  builder: "Лицо, осуществляющее строительство",
  builder_control: "Строительный контроль подрядчика (специалист НРС)",
  designer: "Проектировщик (авторский надзор)",
  performer: "Лицо, выполнившее работы",
};

export const REGISTRIES: Record<string, RegistryDef> = {
  objects: {
    title: "Объекты", path: "/objects", labelKey: "name",
    fields: [
      { key: "name", label: "Наименование по ПД", required: true, list: true },
      { key: "code", label: "Шифр", list: true },
      { key: "address", label: "Адрес", list: true },
      { key: "developer_id", label: "Застройщик / техзаказчик", type: "ref", ref: "counterparties" },
      { key: "designer_id", label: "Проектировщик", type: "ref", ref: "counterparties" },
      { key: "start_date", label: "Начало", type: "date" },
      { key: "end_date", label: "Окончание", type: "date" },
      { key: "status", label: "Статус", type: "select", options: { active: "В работе", paused: "Приостановлен", done: "Завершён" }, list: true },
    ],
  },
  counterparties: {
    title: "Контрагенты", path: "/counterparties", labelKey: "name",
    fields: [
      { key: "name", label: "Наименование", required: true, list: true },
      { key: "kind", label: "Роль", type: "select", list: true, options: {
        developer: "Застройщик / техзаказчик", customer: "Заказчик", designer: "Проектировщик",
        subcontractor: "Субподрядчик", supplier: "Поставщик", other: "Прочее" } },
      { key: "inn", label: "ИНН", list: true },
      { key: "kpp", label: "КПП" },
      { key: "ogrn", label: "ОГРН" },
      { key: "address", label: "Адрес" },
      { key: "phone", label: "Телефон" },
      { key: "email", label: "Email" },
      { key: "sro", label: "СРО (наименование, ОГРН, ИНН)" },
      { key: "head_position", label: "Должность руководителя (дат. падеж)" },
      { key: "head_name", label: "ФИО руководителя (дат. падеж)" },
    ],
  },
  contracts: {
    title: "Договоры", path: "/contracts", labelKey: "number",
    fields: [
      { key: "kind", label: "Вид", type: "select", required: true, list: true, options: {
        general: "Генподряд", subcontract: "Субподряд", supply: "Поставка", services: "Услуги", design: "Проектирование" } },
      { key: "number", label: "Номер", required: true, list: true },
      { key: "date", label: "Дата", type: "date", list: true },
      { key: "subject", label: "Предмет", type: "textarea", list: true },
      { key: "object_id", label: "Объект", type: "ref", ref: "objects" },
      { key: "counterparty_id", label: "Контрагент", type: "ref", ref: "counterparties", list: true },
      { key: "amount", label: "Цена, руб.", type: "number", list: true },
      { key: "start_date", label: "Начало", type: "date" },
      { key: "end_date", label: "Окончание", type: "date", list: true },
      { key: "responsible_id", label: "Ответственный за исполнение", type: "ref", ref: "persons" },
    ],
  },
  persons: {
    title: "Ответственные", path: "/persons", labelKey: "full_name",
    fields: [
      { key: "full_name", label: "ФИО", required: true, list: true },
      { key: "position", label: "Должность", list: true },
      { key: "counterparty_id", label: "Организация (пусто — наша)", type: "ref", ref: "counterparties", list: true },
      { key: "nrs_id", label: "Номер в НРС", list: true },
      { key: "phone", label: "Телефон" },
      { key: "email", label: "Email" },
    ],
  },
  appointments: {
    title: "Назначения (приказы)", path: "/appointments", labelKey: "role",
    fields: [
      { key: "person_id", label: "Лицо", type: "ref", ref: "persons", required: true, list: true },
      { key: "object_id", label: "Объект", type: "ref", ref: "objects", required: true, list: true },
      { key: "role", label: "Роль в ИД", type: "select", options: ROLES, required: true, list: true },
      { key: "order_number", label: "Номер приказа", list: true },
      { key: "order_date", label: "Дата приказа", type: "date", list: true },
      { key: "valid_from", label: "Действует с", type: "date" },
      { key: "valid_to", label: "Действует по", type: "date" },
    ],
  },
  tasks: {
    title: "Поручения", path: "/tasks", labelKey: "title",
    fields: [
      { key: "title", label: "Поручение", type: "textarea", required: true, list: true },
      { key: "assignee_person_id", label: "Исполнитель", type: "ref", ref: "persons", list: true },
      { key: "due_date", label: "Срок", type: "date", list: true },
      { key: "priority", label: "Приоритет", type: "select", options: { low: "Низкий", normal: "Обычный", high: "Высокий" }, list: true },
      { key: "status", label: "Статус", type: "select", options: { open: "Открыто", in_progress: "В работе", done: "Исполнено", cancelled: "Отменено" }, list: true },
      { key: "object_id", label: "Объект", type: "ref", ref: "objects" },
      { key: "contract_id", label: "Договор", type: "ref", ref: "contracts" },
      { key: "description", label: "Описание", type: "textarea" },
      { key: "result", label: "Отчёт об исполнении", type: "textarea" },
    ],
  },
  estimate: {
    title: "Смета (позиции)", path: "/estimate-items", labelKey: "name",
    fields: [
      { key: "contract_id", label: "Договор", type: "ref", ref: "contracts", required: true },
      { key: "object_id", label: "Объект", type: "ref", ref: "objects" },
      { key: "code", label: "№ / шифр", list: true },
      { key: "name", label: "Наименование работ", type: "textarea", required: true, list: true },
      { key: "unit", label: "Ед. изм.", required: true, list: true },
      { key: "quantity", label: "Количество", type: "number", required: true, list: true },
      { key: "unit_price", label: "Цена за ед., руб.", type: "number", list: true },
      { key: "requires_aosr", label: "Скрытые работы (нужен АОСР)", type: "select", options: { true: "Да", false: "Нет" }, list: true },
      { key: "section", label: "Раздел" },
      { key: "ord", label: "Порядок", type: "number" },
    ],
  },
  schedule: {
    title: "ГПР", path: "/schedule", labelKey: "name",
    fields: [
      { key: "object_id", label: "Объект", type: "ref", ref: "objects", required: true },
      { key: "name", label: "Работа", required: true, list: true },
      { key: "estimate_item_id", label: "Позиция сметы", type: "ref", ref: "estimate" },
      { key: "planned_start", label: "Начало", type: "date", required: true, list: true },
      { key: "planned_end", label: "Окончание", type: "date", required: true, list: true },
      { key: "planned_quantity", label: "Объём", type: "number", list: true },
      { key: "unit", label: "Ед.", list: true },
      { key: "responsible_id", label: "Ответственный", type: "ref", ref: "persons", list: true },
    ],
  },
  volumes: {
    title: "Выполненные объёмы", path: "/volumes", labelKey: "location",
    fields: [
      { key: "object_id", label: "Объект", type: "ref", ref: "objects", required: true },
      { key: "estimate_item_id", label: "Позиция сметы", type: "ref", ref: "estimate", required: true, list: true },
      { key: "date", label: "Дата", type: "date", required: true, list: true },
      { key: "quantity", label: "Объём", type: "number", required: true, list: true },
      { key: "location", label: "Оси / захватка", list: true },
      { key: "status", label: "Статус", type: "select", options: { reported: "Заявлено", confirmed: "Подтверждено ПТО", closed: "Закрыто в КС-2" }, list: true },
      { key: "note", label: "Примечание" },
    ],
  },
  materials: {
    title: "Материалы", path: "/materials", labelKey: "name",
    fields: [
      { key: "name", label: "Наименование", required: true, list: true },
      { key: "unit", label: "Ед. изм.", required: true, list: true },
      { key: "code", label: "Код (1С)", list: true },
      { key: "category", label: "Категория", list: true },
    ],
  },
  norms: {
    title: "Нормы расхода", path: "/material-norms", labelKey: "rate",
    fields: [
      { key: "estimate_item_id", label: "Позиция сметы", type: "ref", ref: "estimate", required: true, list: true },
      { key: "material_id", label: "Материал", type: "ref", ref: "materials", required: true, list: true },
      { key: "rate", label: "Норма на ед. работ", type: "number", required: true, list: true },
      { key: "loss_percent", label: "Потери, %", type: "number", list: true },
      { key: "source", label: "Источник нормы", list: true },
    ],
  },
  receipts: {
    title: "Приход материалов", path: "/material-receipts", labelKey: "document_ref",
    fields: [
      { key: "object_id", label: "Объект", type: "ref", ref: "objects", required: true },
      { key: "material_id", label: "Материал", type: "ref", ref: "materials", required: true, list: true },
      { key: "date", label: "Дата", type: "date", required: true, list: true },
      { key: "quantity", label: "Количество", type: "number", required: true, list: true },
      { key: "price", label: "Цена без НДС", type: "number", list: true },
      { key: "document_ref", label: "УПД / накладная", list: true },
      { key: "quality_doc", label: "Документ о качестве", list: true },
      { key: "supplier_id", label: "Поставщик", type: "ref", ref: "counterparties" },
    ],
  },
  usage: {
    title: "Расход материалов", path: "/material-usage", labelKey: "note",
    fields: [
      { key: "object_id", label: "Объект", type: "ref", ref: "objects", required: true },
      { key: "material_id", label: "Материал", type: "ref", ref: "materials", required: true, list: true },
      { key: "date", label: "Дата", type: "date", required: true, list: true },
      { key: "quantity", label: "Количество", type: "number", required: true, list: true },
      { key: "estimate_item_id", label: "На работу", type: "ref", ref: "estimate", list: true },
      { key: "note", label: "Примечание" },
    ],
  },
  obligations: {
    title: "Обязательства", path: "/obligations", labelKey: "description",
    fields: [
      { key: "contract_id", label: "Договор", type: "ref", ref: "contracts", required: true, list: true },
      { key: "description", label: "Обязательство", type: "textarea", required: true, list: true },
      { key: "party", label: "Сторона", type: "select", options: { we: "Мы", counterparty: "Контрагент" }, list: true },
      { key: "due_date", label: "Срок", type: "date", list: true },
      { key: "responsible_id", label: "Ответственный", type: "ref", ref: "persons", list: true },
      { key: "status", label: "Статус", type: "select", options: { open: "Открыто", done: "Исполнено", overdue: "Просрочено" }, list: true },
      { key: "penalty", label: "Ответственность (неустойка)" },
      { key: "source_clause", label: "Пункт договора" },
    ],
  },
};

type Row = Record<string, unknown> & { id: string };

export default function Registry({ kind }: { kind: string }) {
  const def = REGISTRIES[kind];
  const [rows, setRows] = useState<Row[]>([]);
  const [refs, setRefs] = useState<Record<string, Row[]>>({});
  const [form, setForm] = useState<Record<string, string>>({});
  const [editId, setEditId] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");

  const load = () => api<Row[]>(`${def.path}?q=${encodeURIComponent(q)}`).then(setRows).catch((e) => setError(e.message));

  useEffect(() => {
    load();
    const needed = [...new Set(def.fields.filter((f) => f.ref).map((f) => f.ref!))];
    Promise.all(needed.map((r) => api<Row[]>(`${REGISTRIES[r].path}?limit=500`).then((data) => [r, data] as const))).then((pairs) =>
      setRefs(Object.fromEntries(pairs)),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  const refLabel = (ref: string, id: unknown) => {
    const row = refs[ref]?.find((r) => r.id === id);
    return row ? String(row[REGISTRIES[ref].labelKey] ?? "") : "";
  };

  const show = (f: FieldDef, v: unknown) => {
    if (v === null || v === undefined || v === "") return "";
    if (f.type === "ref") return refLabel(f.ref!, v);
    if (f.type === "select") return f.options?.[String(v)] ?? String(v);
    if (f.type === "textarea") return String(v).length > 160 ? String(v).slice(0, 160) + "…" : String(v);
    if (f.type === "number") return Number(v).toLocaleString("ru-RU");
    return String(v);
  };

  function edit(row: Row | null) {
    setEditId(row?.id ?? null);
    const values: Record<string, string> = {};
    for (const f of def.fields) values[f.key] = row ? String(row[f.key] ?? "") : "";
    setForm(values);
  }

  async function save() {
    setError("");
    const body: Record<string, unknown> = {};
    for (const f of def.fields) {
      const v = form[f.key] ?? "";
      if (v === "" && !editId) continue;
      body[f.key] = v === "" ? null : f.type === "number" ? Number(v) : v === "true" ? true : v === "false" ? false : v;
    }
    try {
      if (editId) await api(`${def.path}/${editId}`, { method: "PATCH", json: body });
      else await api(def.path, { method: "POST", json: body });
      setForm({});
      setEditId(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function remove(row: Row) {
    if (!confirm("Удалить запись?")) return;
    try {
      await api(`${def.path}/${row.id}`, { method: "DELETE" });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const formOpen = Object.keys(form).length > 0;
  const listFields = def.fields.filter((f) => f.list);

  return (
    <>
      <h1>{def.title}</h1>
      {error && <div className="alert error">{error}</div>}
      {formOpen && (
        <div className="panel">
          <h2>{editId ? "Редактирование" : "Новая запись"}</h2>
          <div className="row">
            {def.fields.map((f) => (
              <div className="field" key={f.key}>
                <label>
                  {f.label}
                  {f.required ? " *" : ""}
                </label>
                {f.type === "select" || f.type === "ref" ? (
                  <select value={form[f.key] ?? ""} onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}>
                    <option value="">—</option>
                    {f.type === "select"
                      ? Object.entries(f.options ?? {}).map(([k, v]) => (
                          <option key={k} value={k}>
                            {v}
                          </option>
                        ))
                      : (refs[f.ref!] ?? []).map((r) => (
                          <option key={r.id} value={r.id}>
                            {String(r[REGISTRIES[f.ref!].labelKey] ?? r.id)}
                          </option>
                        ))}
                  </select>
                ) : f.type === "textarea" ? (
                  <textarea value={form[f.key] ?? ""} onChange={(e) => setForm({ ...form, [f.key]: e.target.value })} />
                ) : (
                  <input
                    type={f.type === "date" ? "date" : f.type === "number" ? "number" : "text"}
                    value={form[f.key] ?? ""}
                    onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                  />
                )}
              </div>
            ))}
          </div>
          <div className="row">
            <button className="btn primary" onClick={save}>
              Сохранить
            </button>
            <button className="btn" onClick={() => setForm({})}>
              Отмена
            </button>
          </div>
        </div>
      )}
      <div className="panel">
        <div className="row" style={{ marginBottom: 8 }}>
          <div className="field">
            <input placeholder="Поиск" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" onClick={() => edit(null)}>
              Добавить
            </button>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              {listFields.map((f) => (
                <th key={f.key}>{f.label}</th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={listFields.length + 1} className="muted">
                  Записей нет
                </td>
              </tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                {listFields.map((f) => (
                  <td key={f.key}>{show(f, r[f.key])}</td>
                ))}
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn small" onClick={() => edit(r)}>
                    Изменить
                  </button>{" "}
                  <button className="btn small" onClick={() => remove(r)}>
                    Удалить
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// Демо-режим без сервера: ответы API берутся из снимка, записанного с работающего backend BIS
// (scripts/record_demo.py), изменения хранятся в памяти страницы до перезагрузки.
// Снимок встраивается в страницу как window.__BIS_SNAPSHOT__.

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

interface Snapshot {
  get: Record<string, Any>;
  lists: Record<string, Any[]>;
  documents: Any[];
  notifications: Any[];
  memory: Any[];
  users: Any[];
  org: Any;
  actions: Record<string, Any>;
  previews: Record<string, { type: "html" | "svg"; content: string }>;
  chat: { question: string; reply: Any }[];
  chunks: Any[];
  demo_period: [string, string];
  demo_contract_id: string;
  pd_doc_id: string | null;
  ai?: {
    events: Any[];
    specialists: Any;
    files: Record<string, { mime: string; data: string }>;
    samples: { id: string; title: string; hint: string; event: Any }[];
    sample_documents: Any[];
  };
}

class DemoError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let state: Snapshot | null = null;
const sessions: Record<string, { id: string; title: string; created_at: string; messages: Any[] }> = {};
let ks2Done = false;
const added = new Set<string>();

function snap(): Snapshot {
  if (!state) {
    const raw = (window as Any).__BIS_SNAPSHOT__;
    if (!raw) throw new DemoError(500, "Нет данных демо-версии");
    state = structuredClone(raw) as Snapshot;
  }
  return state;
}

const uid = () => (crypto as Any).randomUUID?.() ?? `id-${Math.random().toString(16).slice(2)}-${Date.now()}`;
const now = () => new Date().toISOString();
const UPLOAD_OFF = "В демо-версии загрузка файлов отключена. На сервере файл будет разобран, проиндексирован и обработан агентами.";
const LISTS = ["objects", "counterparties", "contracts", "persons", "appointments", "tasks", "obligations", "estimate-items",
  "schedule", "volumes", "materials", "material-norms", "material-receipts", "material-usage"];

export function isDemoSurvey(points: Any[]): boolean {
  const ref = snap().actions.survey_points as Any[];
  return JSON.stringify(points.map((p) => [p.name, p.design_x, p.design_y])) === JSON.stringify(ref.map((p) => [p.name, p.design_x, p.design_y]));
}

export function demoSurvey(): Any[] {
  return structuredClone(snap().actions.survey_points);
}

function parseQuery(path: string): [string, URLSearchParams] {
  const [p, q] = path.split("?");
  return [p, new URLSearchParams(q ?? "")];
}

function inPeriod(body: Any): boolean {
  const [s, e] = snap().demo_period;
  return body?.period_start <= e && body?.period_end >= s;
}

function addDocument(doc: Any): Any {
  const s = snap();
  let d = doc;
  if (added.has(doc.id)) {
    // повторное формирование — новый номер, тот же файл
    const n = Number(doc.number) || 1;
    d = { ...doc, id: uid(), number: String(n + s.documents.filter((x) => x.kind === doc.kind).length),
      created_at: now(), title: doc.title.replace(/№ \d+/, `№ ${n + s.documents.filter((x) => x.kind === doc.kind).length}`) };
    s.previews[d.id] = s.previews[doc.id];
  }
  added.add(d.id);
  s.documents.unshift({ ...d, created_at: now() });
  return d;
}

// --- поиск по базе знаний (на клиенте) --------------------------------------------------------

const stem = (w: string) => (w.length <= 4 ? w : w.slice(0, Math.max(4, w.length - 2)));
const words = (t: string) => (t.toLowerCase().replace(/ё/g, "е").match(/[a-zа-я0-9]+/g) ?? []);

export function searchChunks(query: string, scope = "all", k = 6): Any[] {
  const qs = [...new Set(words(query).filter((w) => w.length > 2 || /\d/.test(w)).map(stem))];
  const ref = /п\.\s*([а-яa-z]?\.?\d+(?:\.\d+)*)/i.exec(query)?.[1];
  const pool = snap().chunks.filter((c) => scope === "all" || (scope === "norms" ? c.kind === "norm" : c.kind !== "norm"));
  const docs = pool.map((c) => words(`${c.section_path ?? ""} ${c.heading ?? ""} ${c.content}`));
  // вес слова — обратная частота: редкие слова запроса важнее частых («бетон» встречается почти везде)
  const idf = (q: string) => Math.log(1 + pool.length / (1 + docs.filter((ws) => ws.some((w) => w.startsWith(q))).length));
  const weights = Object.fromEntries(qs.map((q) => [q, idf(q)]));
  const scored = pool.map((c, i) => {
    let score = 0;
    for (const q of qs) {
      if (docs[i].some((w) => w.startsWith(q))) score += weights[q];
      else if (q.length >= 5 && docs[i].some((w) => w.startsWith(q.slice(0, 4)))) score += weights[q] * 0.4;
    }
    if (ref && c.clause && (c.clause === ref || c.clause.startsWith(ref + "."))) score += 10;
    return { c, score };
  }).filter((x) => x.score > 0).sort((a, b) => b.score - a.score).slice(0, k);
  return scored.map((x, i) => ({ ...x.c, n: i + 1, score: Math.round(x.score * 100) / 100 }));
}

// --- чат -------------------------------------------------------------------------------------

const ROUTES: [string, string[]][] = [
  ["closing", ["кс-2", "кс-3", "кс2", "кс3", "закрыт", "закрыва", "акт выполненных", "процентовк"]],
  ["materials", ["материал", "списани", "м-29", "м29", "перерасход", "расход", "остат", "приход", "упд"]],
  ["works", ["план-факт", "план факт", "отстава", "гпр", "график", "залили", "смонтир", "объём", "объем"]],
  ["cad", ["схем", "геодез", "съёмк", "съемк", "dxf", "dwg", "чертеж", "чертёж"]],
  ["project", ["проектн", "комплектност", "спецификац", "ведомость замечаний"]],
  ["pto", ["аоср", "освидетельств", "скрыт", "исполнительную документ", "исполнительной документ"]],
  ["secretary", ["письм", "входящ", "исходящ", "ответ на"]],
  ["contracts", ["договор", "обязательств", "неустойк", "пени", "претензи", "аванс"]],
  ["control", ["просроч", "поручени", "кто отвечает", "напомн", "задач"]],
];
const TITLES: Record<string, string> = {
  norms: "Нормоконтроль", secretary: "Секретарь", pto: "ПТО / Исполнительная документация", contracts: "Договорной",
  control: "Контроль исполнения", works: "Контроль работ", materials: "Материалы", closing: "Закрытие работ",
  cad: "Исполнительные схемы (CAD)", project: "Анализ проектной документации", assistant: "Помощник",
};

function route(text: string): string {
  const low = text.toLowerCase();
  for (const [agent, keys] of ROUTES) if (keys.some((k) => low.includes(k))) return agent;
  return "norms";
}

function overlap(a: string, b: string): number {
  const sa = new Set(words(a).map(stem));
  return words(b).map(stem).filter((w) => sa.has(w)).length;
}

function chat(body: Any): Any {
  const s = snap();
  const text: string = body.message;
  const norm = (t: string) => words(t).join(" ");
  let reply = s.chat.find((c) => norm(c.question) === norm(text))?.reply;
  const agent = body.agent || reply?.agent || route(text);
  if (!reply) {
    const same = s.chat.filter((c) => c.reply.agent === agent).sort((a, b) => overlap(b.question, text) - overlap(a.question, text));
    if (["control", "works", "materials", "closing", "contracts", "project"].includes(agent) && same.length) {
      reply = { ...same[0].reply, text: same[0].reply.text + "\n\n(Демо: показан ответ агента на ближайший типовой запрос.)" };
    } else if (["pto", "secretary", "cad"].includes(agent)) {
      const form = { pto: "«Новый АОСР»", secretary: "«Документы»", cad: "«Исполнительные схемы»" }[agent as "pto" | "secretary" | "cad"];
      reply = { agent, text: `Агент «${TITLES[agent]}» составляет документы из чата, когда подключена языковая модель ` +
        `(локальная, GigaChat или YandexGPT). В демо-версии модели нет — откройте раздел ${form}: там тот же результат формируется по форме.`,
        citations: [], documents: [], trace: [] };
    } else {
      const hits = searchChunks(text, "all", 5);
      reply = hits.length
        ? { agent: "norms", citations: hits, documents: [], trace: [{ tool: "search_knowledge", error: false }],
            text: "Офлайн-режим: ответ собран из базы знаний без языковой модели.\n\nНайденные положения (проверьте применимость):\n" +
              hits.map((h: Any) => `[${h.n}] ${h.citation}: ${h.content.replace(/\s+/g, " ").slice(0, 400)}`).join("\n") }
        : { agent: "norms", citations: [], documents: [], trace: [], text: "В базе знаний ничего не найдено по запросу." };
    }
  }
  const sessionId = body.session_id || uid();
  if (!sessions[sessionId]) sessions[sessionId] = { id: sessionId, title: text.slice(0, 80), created_at: now(), messages: [] };
  const out = { ...reply, session_id: sessionId, agent_title: TITLES[reply.agent] ?? reply.agent,
    routed_by: body.agent ? "выбран пользователем" : "по ключевым словам", provider: "offline", model: "offline" };
  sessions[sessionId].messages.push({ role: "user", content: text, created_at: now() },
    { role: "assistant", content: out.text, agent: out.agent, meta: out, created_at: now() });
  return out;
}

// --- реестры -----------------------------------------------------------------------------------

function listRoute(method: string, base: string, id: string | undefined, q: URLSearchParams, body: Any): Any {
  const list = snap().lists[base];
  if (method === "GET" && !id) {
    let rows = [...list];
    const ff = q.get("filter_field"), fv = q.get("filter_value"), text = q.get("q");
    if (ff && fv) rows = rows.filter((r) => String(r[ff]) === fv);
    if (text) rows = rows.filter((r) => JSON.stringify(r).toLowerCase().includes(text.toLowerCase()));
    return rows.slice(0, Number(q.get("limit") ?? 100));
  }
  const idx = list.findIndex((r) => r.id === id);
  if (method === "GET") {
    if (idx < 0) throw new DemoError(404, "Запись не найдена");
    return list[idx];
  }
  if (method === "POST") {
    const row = { ...body, id: uid(), created_at: now() };
    list.unshift(row);
    return row;
  }
  if (method === "PATCH") {
    list[idx] = { ...list[idx], ...body };
    return list[idx];
  }
  if (method === "DELETE") {
    list.splice(idx, 1);
    return undefined;
  }
}

function summary(): Any {
  const s = snap();
  const today = new Date().toISOString().slice(0, 10);
  const week = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10);
  const ob = s.lists.obligations, tk = s.lists.tasks;
  const active = (o: Any) => ["open", "overdue"].includes(o.status);
  return {
    obligations_overdue: ob.filter((o) => active(o) && o.due_date && o.due_date < today).length,
    obligations_week: ob.filter((o) => o.status === "open" && o.due_date && o.due_date >= today && o.due_date <= week).length,
    obligations_no_responsible: ob.filter((o) => active(o) && o.party === "we" && !o.responsible_id).length,
    tasks_overdue: tk.filter((t) => ["open", "in_progress"].includes(t.status) && t.due_date && t.due_date < today).length,
    tasks_open: tk.filter((t) => ["open", "in_progress"].includes(t.status)).length,
    notifications_unread: s.notifications.filter((n) => !n.is_read).length,
  };
}

function penalty(b: Any): Any {
  let value: number, formula: string;
  if (b.rate_percent_per_day != null) {
    value = b.base_amount * b.rate_percent_per_day / 100 * b.days;
    formula = `${b.base_amount} × ${b.rate_percent_per_day}% × ${b.days} дн.`;
  } else if (b.key_rate_percent != null && b.key_rate_fraction) {
    const [n, d] = String(b.key_rate_fraction).split("/").map(Number);
    value = b.base_amount * b.key_rate_percent / 100 * n / d * b.days;
    formula = `${b.base_amount} × ${b.key_rate_fraction} × ${b.key_rate_percent}% × ${b.days} дн.`;
  } else throw new DemoError(400, "Укажите ставку в % за день или ключевую ставку и долю (например, 1/300)");
  if (b.cap_percent != null && b.cap_percent > 0 && value > b.base_amount * b.cap_percent / 100) {
    value = b.base_amount * b.cap_percent / 100;
    formula += `, ограничено ${b.cap_percent}% суммы`;
  }
  return { ...b, penalty: Math.round(value * 100) / 100, formula };
}

// --- маршрутизатор ---------------------------------------------------------------------------

export async function mockApi<T>(path: string, method: string, body: Any): Promise<T> {
  await new Promise((r) => setTimeout(r, 120));  // ощущение сети
  return (await route_(path, method.toUpperCase(), body)) as T;
}

function route_(path: string, method: string, body: Any): Any {
  const s = snap();
  const [p, q] = parseQuery(path);
  const parts = p.split("/").filter(Boolean);
  const a = s.actions;

  if (p.startsWith("/ai/") || p === "/intake" || p.startsWith("/telegram/")) return aiRoute(p, q, method, body);
  if (body instanceof FormData) {
    if (p === "/schemes/survey") return parseSurvey(body);
    if (p === "/voice/transcribe") throw new DemoError(503, "Распознавание речи не подключено в демо-версии");
    throw new DemoError(400, UPLOAD_OFF);
  }
  if (p === "/chat" && method === "POST") return chat(body);
  if (p === "/chat/sessions") return Object.values(sessions).reverse().map(({ id, title, created_at }) => ({ id, title, created_at }));
  if (parts[0] === "chat" && parts[1] === "sessions") return sessions[parts[2]]?.messages ?? [];
  if (p === "/control/summary") return summary();
  if (p === "/notifications") return s.notifications.slice(0, Number(q.get("limit") ?? 50));
  if (p === "/notifications/read-all") { s.notifications.forEach((n) => (n.is_read = true)); return undefined; }
  if (parts[0] === "notifications" && parts[2] === "read") { const n = s.notifications.find((x) => x.id === parts[1]); if (n) n.is_read = true; return undefined; }
  if (parts[0] === "jobs" && parts[2] === "run") return a[`job_${parts[1]}`] ?? { name: parts[1], status: "ok", result: {} };
  if (p === "/org" && method === "PATCH") { s.org = { ...s.org, ...body }; return s.org; }
  if (p === "/org") return s.org;
  if (p === "/memory" && method === "POST") { const m = { ...body, id: uid(), source: "demo", created_at: now() }; s.memory.unshift(m); return m; }
  if (p === "/memory") return s.memory;
  if (parts[0] === "memory" && method === "PATCH") { const m = s.memory.find((x) => x.id === parts[1]); Object.assign(m, body); return m; }
  if (p === "/users" && method === "POST") { const u = { ...body, id: uid(), is_active: true, is_platform_admin: false }; delete u.password; s.users.push(u); return u; }
  if (p === "/users") return s.users;
  if (p === "/contracts/penalty") return penalty(body);
  if (p === "/knowledge/search") return { query: q.get("q"), results: searchChunks(q.get("q") ?? "", q.get("scope") ?? "all", Number(q.get("k") ?? 8)) };

  // документы
  if (p === "/documents" && method === "GET") {
    return s.documents.filter((d) => (!q.get("kind") || d.kind === q.get("kind")) && (!q.get("status") || d.status === q.get("status"))
      && (!q.get("object_id") || d.object_id === q.get("object_id")));
  }
  if (p === "/documents/aosr/check") return a.aosr_check;
  if (p === "/documents/aosr") return addDocument(a.aosr_doc);
  if (p === "/documents/letter") return addDocument(a.letter_doc);
  if (parts[0] === "documents" && parts.length === 2 && method === "GET" && s.documents.some((d) => d.id === parts[1])) {
    return s.documents.find((d) => d.id === parts[1]);
  }
  if (parts[0] === "documents" && parts[2] === "approve") {
    const d = s.documents.find((x) => x.id === parts[1]);
    const errors = (d.issues ?? []).filter((i: Any) => i.level === "error");
    if (errors.length) throw new DemoError(400, "Есть ошибки заполнения: " + errors.map((i: Any) => i.message).join("; "));
    Object.assign(d, { status: "approved", approved_at: now(), reject_reason: null });
    return d;
  }
  if (parts[0] === "documents" && parts[2] === "reject") {
    const d = s.documents.find((x) => x.id === parts[1]);
    Object.assign(d, { status: "rejected", reject_reason: body.reason });
    if (String(d.created_by ?? "").startsWith("agent:")) {
      s.memory.unshift({ id: uid(), kind: "feedback", scope: d.created_by.slice(6), content: `Замечание к документу «${d.title}»: ${body.reason}`,
        source: `document:${d.id}`, is_active: true, created_at: now() });
    }
    return d;
  }
  if (parts[0] === "documents" && parts[2] === "compare-estimate") {
    if (parts[1] !== s.pd_doc_id) throw new DemoError(400, "В демо-версии сверка доступна для документа «ЖК-01-КЖ1» со спецификацией");
    return a.compare_estimate;
  }
  if (parts[0] === "documents" && parts[2] === "specification") return a.specification ?? { items: [] };

  // закрытие, материалы, схемы
  if (parts[0] === "contracts" && parts[2] === "closing") {
    const empty = { rows: [], issues: [{ level: "warning", field: "period", message: "В демо-данных объёмы работ есть за май 2026 — выберите период 01.05.2026–31.05.2026" }], total: 0, vat: 0, total_with_vat: 0 };
    const ok = parts[1] === s.demo_contract_id && inPeriod(body);
    if (parts[3] === "check") return ok && !ks2Done ? a.closing_check : ok ? { ...empty, issues: [{ level: "warning", field: "period", message: "Подтверждённые объёмы за период уже закрыты в КС-2" }] } : empty;
    if (!ok || ks2Done) throw new DemoError(400, "Нет объёмов, которые можно закрыть за период");
    ks2Done = true;
    addDocument(a.ks2.ks2);
    addDocument(a.ks2.ks3);
    return a.ks2;
  }
  if (p === "/materials/m29/preview") return inPeriod(body) ? a.m29_preview : { rows: [], issues: [{ level: "warning", field: "period", message: "В демо-данных расход материалов есть за май 2026" }] };
  if (p === "/materials/m29") return addDocument(a.m29_doc);
  if (p === "/materials/write-off") return addDocument(a.writeoff_doc);
  if (p === "/schemes") {
    if (!isDemoSurvey(body.points ?? [])) throw new DemoError(400, "В демо-версии схема строится по примеру съёмки (кнопка «Пример съёмки»). На сервере — по любому файлу геодезиста.");
    return addDocument(a.scheme_doc);
  }
  if (parts[0] === "contracts" && parts[2] === "analyze") return { ...(s.lists.contracts.find((c) => c.id === parts[1])?.extra?.analysis ?? {}), obligations_added: 0 };

  // реестры
  if (LISTS.includes(parts[0]) && parts.length <= 2) return listRoute(method, parts[0], parts[1], q, body);

  // прочие GET — из снимка
  if (method === "GET") {
    const exact = s.get[path] ?? s.get[p];
    if (exact !== undefined) return structuredClone(exact);
  }
  throw new DemoError(404, "Этот раздел недоступен в демо-версии");
}

async function parseSurvey(form: FormData): Promise<Any> {
  const file = form.get("file") as File | null;
  if (!file || !/\.(csv|txt)$/i.test(file.name)) throw new DemoError(400, "В демо-версии принимается CSV/TXT; XLSX — на сервере");
  const text = await file.text();
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  const sep = lines[0].includes(";") ? ";" : lines[0].includes("\t") ? "\t" : ",";
  const num = (v: string | undefined) => (v === undefined || v.trim() === "" ? null : Number(v.replace(",", ".")));
  const start = /[a-zа-я]/i.test(lines[0].split(sep)[1] ?? "") ? 1 : 0;
  const points = lines.slice(start).map((l) => l.split(sep)).filter((c) => c.length >= 3).map((c) => ({
    name: c[0].trim(), design_x: num(c[1]), design_y: num(c[2]), fact_x: num(c[3]), fact_y: num(c[4]), design_z: num(c[5]), fact_z: num(c[6]),
  }));
  return { points };
}

export async function mockBlob(path: string): Promise<Blob> {
  const s = snap();
  if (path.startsWith("/intake/")) {
    const f = s.ai?.files[path.split("/")[2]];
    if (!f) throw new Error("Файл недоступен в демо-версии");
    const bin = atob(f.data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: f.mime });
  }
  const id = path.split("/")[2];
  const pv = s.previews[id];
  if (path.endsWith("/preview") && pv?.type === "svg") return new Blob([pv.content], { type: "image/svg+xml" });
  throw new Error("Недоступно в демо-версии");
}

export function previewFor(path: string): { type: "html" | "svg"; content: string; title: string } | null {
  const s = snap();
  const id = path.split("/")[2];
  const doc = s.documents.find((d) => d.id === id);
  const pv = s.previews[id];
  if (!pv) return null;
  return { ...pv, title: doc?.title ?? "Документ" };
}

// --- ИИ-конвейер в демо ------------------------------------------------------------------------
// Лента и примеры записаны с настоящего сервера (scripts/demo_ai_scenario.py, record_demo.py).
// Решения людей меняют данные в памяти страницы; свой текст прораба разбирается упрощённо в браузере.

const EDITABLE: Record<string, string[]> = {
  create_receipt: ["material_id", "quantity", "price", "object_id", "date", "quality_doc"],
  report_volume: ["estimate_item_id", "quantity", "location", "object_id", "date"],
  record_usage: ["material_id", "quantity", "object_id", "date"],
  create_task: ["title", "role", "due_days", "priority"],
  draft_aosr: ["works", "project_docs", "materials", "executive_docs", "norms", "next_works"],
  import_contract: ["kind", "our_role", "object_id"],
  link_certificate: ["receipt_ids", "text"],
};
const KIND_TITLES: Record<string, string> = {
  report_volume: "Выполненный объём", record_usage: "Расход материала", create_task: "Поручение", flag_issue: "Замечание",
  info: "Сведения", create_receipt: "Приход материала",
};
const SPEC_TITLES: Record<string, string> = { dispatcher: "Диспетчер", hearing: "Слух", verifier: "Контролёр", reader: "Чтец документов" };
let autonomyModes: Record<string, string> | null = null;

function ai() {
  const s = snap();
  if (!s.ai) throw new DemoError(404, "Лента ИИ не записана в этой демо-версии");
  return s.ai;
}

function allActions(): Any[] {
  return ai().events.flatMap((e: Any) => e.actions);
}

function findAction(id: string): [Any, Any] {
  for (const e of ai().events) {
    const a = e.actions.find((x: Any) => x.id === id);
    if (a) return [e, a];
  }
  throw new DemoError(404, "Действие не найдено");
}

function feed(q: URLSearchParams): Any {
  let evs = ai().events;
  const only = q.get("only") ?? "all";
  if (only === "pending") evs = evs.filter((e: Any) => e.actions.some((a: Any) => ["proposed", "failed"].includes(a.status)));
  if (only === "issues") evs = evs.filter((e: Any) => e.actions.some((a: Any) => a.kind === "flag_issue" && a.severity !== "info"));
  if (q.get("object_id")) evs = evs.filter((e: Any) => e.object_id === q.get("object_id"));
  if (q.get("specialist")) evs = evs.filter((e: Any) => e.actions.some((a: Any) => a.specialist === q.get("specialist")));
  const pending = allActions().filter((a) => ["proposed", "failed"].includes(a.status)).length;
  return { events: structuredClone(evs.slice(0, Number(q.get("limit") ?? 30))), pending };
}

function avgPrice(materialId: string, exceptId: string): number | null {
  const rows = snap().lists["material-receipts"].filter((r) => r.material_id === materialId && r.id !== exceptId && r.price != null);
  const qty = rows.reduce((t, r) => t + Number(r.quantity), 0);
  return qty ? rows.reduce((t, r) => t + Number(r.quantity) * Number(r.price), 0) / qty : null;
}

function act(ev: Any, specialist: string, kind: string, title: string, summary: string, extra: Any = {}): Any {
  const a = {
    id: uid(), event_id: ev.id, specialist, specialist_title: SPEC_TITLES[specialist] ?? specialist, kind,
    kind_title: KIND_TITLES[kind] ?? kind, title, summary, payload: {}, evidence: [], confidence: 0.9, severity: "info",
    status: "applied", auto: true, corrected: false, result: {}, object_id: ev.object_id, reviewed_at: null, reason: null,
    created_at: now(), editable: EDITABLE[kind] ?? [], ...extra,
  };
  ev.actions.push(a);
  return a;
}

/** Применить действие к данным страницы (то, что на сервере делает конвейер). */
function applyDemo(ev: Any, a: Any): void {
  const s = snap();
  const p = a.payload;
  if (a.kind === "create_receipt") {
    let materialId = p.material_id;
    if (!materialId && p.new_material) {
      const m = { id: uid(), name: p.new_material.name, unit: p.new_material.unit, code: null, category: "Из накладных (ИИ)" };
      s.lists.materials.push(m);
      materialId = m.id;
      a.result.material_created = true;
    }
    const r = { id: uid(), object_id: p.object_id, material_id: materialId, date: p.date, quantity: p.quantity, price: p.price,
      document_ref: p.document_ref, quality_doc: p.quality_doc, supplier_id: p.supplier_id ?? null, created_at: now() };
    s.lists["material-receipts"].unshift(r);
    a.result = { ...a.result, receipt_id: r.id, material_id: materialId };
    const avg = p.price ? avgPrice(materialId, r.id) : null;
    if (avg && p.price > avg * 1.15) {
      const name = s.lists.materials.find((m) => m.id === materialId)?.name ?? "материал";
      act(ev, "verifier", "flag_issue", `Цена выше обычной: ${name}`,
        `${p.price.toLocaleString("ru-RU")} руб. против средней ${Math.round(avg).toLocaleString("ru-RU")} руб. (+${Math.round((p.price / avg - 1) * 100)}%) — проверьте договор поставки.`,
        { severity: "warning", specialist_title: "Контролёр" });
    }
  } else if (a.kind === "report_volume") {
    const v = { id: uid(), object_id: p.object_id, estimate_item_id: p.estimate_item_id, date: p.date, quantity: p.quantity,
      location: p.location, status: "reported", reported_by: `ИИ (${a.specialist_title})`, note: p.note, created_at: now() };
    s.lists.volumes.unshift(v);
    a.result = { volume_id: v.id };
  } else if (a.kind === "record_usage") {
    const u = { id: uid(), object_id: p.object_id, material_id: p.material_id, date: p.date, quantity: p.quantity, note: p.note, created_at: now() };
    s.lists["material-usage"].unshift(u);
    a.result = { usage_id: u.id };
  } else if (a.kind === "create_task") {
    const due = new Date(Date.now() + Number(p.due_days ?? 2) * 864e5).toISOString().slice(0, 10);
    const t = { id: uid(), title: p.title, status: "open", priority: p.priority ?? "normal", due_date: due, object_id: p.object_id,
      source_type: "agent", created_by: `agent:${a.specialist}`, created_at: now() };
    s.lists.tasks.unshift(t);
    a.result = { ...a.result, task_id: t.id };
  } else if (a.kind === "link_certificate") {
    for (const id of p.receipt_ids ?? []) {
      const r = s.lists["material-receipts"].find((x) => x.id === id);
      if (r && !r.quality_doc) r.quality_doc = p.text;
    }
    a.result = { receipt_ids: p.receipt_ids };
  } else if (a.kind === "import_contract") {
    throw new DemoError(400, "В демо-версии договор не импортируется: на сервере юрист извлечёт сроки, суммы и неустойки и поставит обязательства на контроль.");
  }
}

function undoDemo(a: Any): void {
  const s = snap();
  const r = a.result ?? {};
  const drop = (list: string, id?: string) => {
    if (!id) return;
    const i = s.lists[list].findIndex((x) => x.id === id);
    if (i >= 0) s.lists[list].splice(i, 1);
  };
  drop("material-receipts", r.receipt_id);
  drop("volumes", r.volume_id);
  drop("material-usage", r.usage_id);
  if (r.material_created) drop("materials", r.material_id);
  const task = s.lists.tasks.find((t) => t.id === r.task_id);
  if (task && ["open", "in_progress"].includes(task.status)) task.status = "cancelled";
  const doc = s.documents.find((d) => d.id === r.document_id);
  if (doc && ["draft", "registered"].includes(doc.status) && ["draft_aosr", "build_scheme", "register_document"].includes(a.kind)) {
    Object.assign(doc, { status: "rejected", reject_reason: "Отклонено в ленте ИИ" });
  }
}

function review(id: string, body: Any): Any {
  const [ev, a] = findAction(id);
  if (a.reviewed_at || a.status === "rejected") throw new DemoError(400, "Действие уже рассмотрено");
  const s = snap();
  if (body.approve) {
    const changes = Object.fromEntries(Object.entries(body.changes ?? {}).filter(([k]) => (EDITABLE[a.kind] ?? []).includes(k)));
    if (Object.keys(changes).length) {
      if (a.status === "applied") undoDemo(a);
      if (changes.material_id && a.payload.source_name) {
        s.memory.unshift({ id: uid(), kind: "alias", scope: "alias:materials", content: a.payload.source_name,
          source: changes.material_id, is_active: true, created_at: now() });
      }
      a.payload = { ...a.payload, ...changes, ...(changes.material_id ? { new_material: null } : {}) };
      a.corrected = true;
      a.status = "proposed";
    }
    if (["proposed", "failed"].includes(a.status)) {
      applyDemo(ev, a);
      a.status = "applied";
      a.auto = false;
    }
  } else {
    if (a.status === "applied") undoDemo(a);
    a.status = "rejected";
    if (body.reason) {
      s.memory.unshift({ id: uid(), kind: "feedback", scope: a.specialist, content: `«${a.title}» отклонено: ${body.reason}`,
        source: `ai_action:${a.id}`, is_active: true, created_at: now() });
    }
  }
  a.reviewed_at = now();
  a.reason = body.reason ?? null;
  return { action: structuredClone(a), created: [] };
}

function stats(): Record<string, Any> {
  const out: Record<string, Any> = {};
  for (const a of allActions()) {
    const st = (out[a.specialist] ??= { total: 0, auto: 0, approved: 0, corrected: 0, rejected: 0, pending: 0, failed: 0 });
    st.total += 1;
    if (a.status === "rejected") st.rejected += 1;
    else if (a.status === "proposed") st.pending += 1;
    else if (a.status === "failed") st.failed += 1;
    else if (a.corrected) st.corrected += 1;
    else if (a.reviewed_at) st.approved += 1;
    else if (a.auto) st.auto += 1;
  }
  for (const st of Object.values(out)) {
    const decided = st.approved + st.corrected + st.rejected;
    st.accuracy = decided ? Math.round((st.approved / decided) * 1000) / 10 : null;
  }
  return out;
}

function specialists(): Any {
  const base = structuredClone(ai().specialists);
  autonomyModes ??= Object.fromEntries(base.autonomy.map((x: Any) => [x.kind, x.mode]));
  const st = stats();
  base.specialists = base.specialists.map((x: Any) => ({ ...x, stats: st[x.name] ?? {} }));
  base.autonomy = base.autonomy.map((x: Any) => ({ ...x, mode: autonomyModes![x.kind] ?? x.mode }));
  return base;
}

// упрощённый разбор сообщения прораба (на сервере — полноценный конвейер с моделями и правилами)
const stems5 = (t: string) => new Set((t.toLowerCase().replace(/ё/g, "е").match(/[a-zа-я0-9]+/g) ?? []).filter((w) => w.length > 2 || /\d/.test(w)).map((w) => w.slice(0, 5)));
function similarity(a: string, b: string): number {
  const sa = stems5(a), sb = stems5(b);
  if (!sa.size || !sb.size) return 0;
  const inter = [...sa].filter((x) => sb.has(x)).length;
  return (inter / new Set([...sa, ...sb]).size) * 0.5 + (inter / sa.size) * 0.5;
}
const UNIT = "шт|м3|м2|м³|м²|п\\.\\s?м|т|кг|компл|м|л";
const normUnit = (u: string) => u.toLowerCase().replace(/\s|\./g, "").replace("м³", "м3").replace("м²", "м2").replace(/^пм$/, "п.м");
const QTY = new RegExp(`(?<![\\wа-яё.,])((?:\\d{1,3}(?:[ \\u00a0]\\d{3})+|\\d+)(?:[.,]\\d+)?)\\s*(${UNIT})(?![а-яa-z0-9])`, "i");
const LOC = /(захватк\S*\s*№?\s*\d+|в\s+осях\s+[^\s,.]+|\d+\s*этаж\S*|этаж\S*\s*\d+)/i;
const PROBLEM = /не привез|не завез|простой|простаива|сломал|поломк|не хватает|нехватк|задерж|авари|травм|проблем|не было/i;
const USAGE = /израсходова\S*|ушл[оаи]|расход\S*|использова\S*|потрати\S*/i;
const COMPLETE = /закончил|заверш|готов\S* к (?:при[её]мк|освидетельств)|предъяв/i;

function newEvent(text: string | null, objectId: string | null, user: Any, extra: Any = {}): Any {
  const s = snap();
  const objects = s.lists.objects.filter((o) => o.status === "active");
  const obj = objects.find((o) => o.id === objectId) ?? (objects.length === 1 ? objects[0] : null);
  return {
    id: uid(), created_at: now(), source: "upload", kind: "text", text, file_name: null, mime: null, file_url: null,
    object_id: obj?.id ?? null, object_name: obj?.name ?? null, sender: user?.full_name ?? null, status: "done",
    detected_type: "foreman_report", dispatcher: { type: "foreman_report", title: "Отчёт с площадки о выполненных работах, расходе, проблемах", confidence: 0.8, by: "rules" },
    transcript: null, excerpt: (text ?? "").slice(0, 600), reply: null, needs_ocr: false, vision: null, error: null, document_id: null, actions: [],
    ...extra,
  };
}

/** Правила (без ИИ): упрощённый разбор сообщения прораба. */
function understandRules(text: string, objectId: string | null, user: Any): Any {
  const s = snap();
  const ev = newEvent(text, objectId, user);
  const today = new Date().toISOString().slice(0, 10);
  const items = s.lists["estimate-items"].filter((i) => !ev.object_id || i.object_id === ev.object_id);
  for (const sent of text.split(/(?<=[.!?;])\s+|\n/).map((x) => x.trim()).filter(Boolean)) {
    const q = QTY.exec(sent);
    if (PROBLEM.test(sent)) {
      act(ev, "dispatcher", "create_task", `Проблема на объекте: ${sent.slice(0, 150)}`, "Поручение руководителю участка.",
        { payload: { title: sent, role: "manager", due_days: 1, priority: "high", object_id: ev.object_id }, status: "new", confidence: 0.85 });
      continue;
    }
    const verb = USAGE.exec(sent);
    if (verb && q) {
      const clean = (t: string) => t.replace(QTY, "").replace(/^\s*(на|по)\s.*$/i, "").replace(/^[\s,.:—-]+|[\s,.:—-]+$/g, "").trim();
      const name = clean(sent.slice(verb.index + verb[0].length)) || clean(sent.slice(0, verb.index)).replace(/^(сегодня|вчера|всего|итого)\s+/i, "");
      const unit = normUnit(q[2]);
      // материал по норме работы из того же сообщения весит больше: «бетона ушло» — бетон той работы, что залили
      const prefer = new Set(s.lists["material-norms"].filter((n) => ev.actions.some((a: Any) => a.kind === "report_volume"
        && a.payload.estimate_item_id === n.estimate_item_id)).map((n) => n.material_id));
      const cands = s.lists.materials.filter((m) => normUnit(m.unit) === unit)
        .map((m) => [m, Math.min(1, similarity(name, m.name) + (prefer.has(m.id) && similarity(name, m.name) > 0 ? 0.15 : 0))] as [Any, number])
        .sort((x, y) => y[1] - x[1]);
      const [mat, score] = cands[0] ?? [null, 0];
      const qty = Number(q[1].replace(/[\s ]/g, "").replace(",", "."));
      if (!mat || score < 0.3) {
        act(ev, "dispatcher", "flag_issue", `Материал не найден в справочнике: ${name}`, `Расход ${qty} ${unit} не учтён — выберите материал, ИИ запомнит название.`, { severity: "warning", status: "applied" });
      } else {
        act(ev, "dispatcher", "record_usage", `Расход: ${mat.name} — ${qty} ${mat.unit}`, `Из сообщения прораба. Совпадение со справочником ${Math.round(score * 100)}%.`,
          { payload: { object_id: ev.object_id, material_id: mat.id, quantity: qty, date: today, note: "из отчёта с площадки", source_name: name }, status: "new", confidence: Math.min(0.95, 0.5 + score * 0.6) });
      }
      continue;
    }
    if (q) {
      const unit = normUnit(q[2]);
      const qty = Number(q[1].replace(/[\s ]/g, "").replace(",", "."));
      const cands = items.filter((i) => normUnit(i.unit) === unit).map((i) => [i, similarity(sent, `${i.code ?? ""} ${i.name} ${i.section ?? ""}`)] as [Any, number]).sort((x, y) => y[1] - x[1]);
      const [item, score] = cands[0] ?? [null, 0];
      const loc = LOC.exec(sent)?.[1] ?? null;
      if (!item || score < 0.2) {
        act(ev, "dispatcher", "flag_issue", `Не сопоставлено со сметой: ${sent.slice(0, 120)}`, "Не найдена позиция сметы с такой единицей — исправьте позицию в ленте, ИИ запомнит.", { severity: "warning" });
      } else {
        act(ev, "dispatcher", "report_volume", `${item.name}: ${qty} ${item.unit}${loc ? `, ${loc}` : ""}`,
          `Из сообщения: «${sent.slice(0, 200)}». Позиция сметы ${item.code ?? ""} (совпадение ${Math.round(score * 100)}%).` + (COMPLETE.test(text) && item.requires_aosr ? " На сервере инженер ПТО сразу соберёт АОСР по этой захватке." : ""),
          { payload: { object_id: ev.object_id ?? item.object_id, estimate_item_id: item.id, quantity: qty, location: loc, date: today, note: sent, source_text: sent },
            status: "new", confidence: Math.min(0.95, 0.55 + score * 0.6), evidence: [{ type: "estimate_item", id: item.id, label: `${item.code ?? ""} ${item.name}`.trim() }] });
      }
    }
  }
  return finalize(ev, "Понял: отчёт с площадки.");
}

function finalize(ev: Any, replyHead: string): Any {
  const s = snap();
  if (!ev.actions.length) act(ev, "dispatcher", "info", "Сообщение сохранено", "Работ, расхода или проблем не найдено.");
  // контроль списания: расход против нормы на сообщённый объём
  for (const u of ev.actions.filter((a: Any) => a.kind === "record_usage")) {
    let norm = 0;
    const parts: string[] = [];
    for (const v of ev.actions.filter((a: Any) => a.kind === "report_volume")) {
      const n = s.lists["material-norms"].find((x) => x.estimate_item_id === v.payload.estimate_item_id && x.material_id === u.payload.material_id);
      if (!n) continue;
      norm += v.payload.quantity * Number(n.rate) * (1 + Number(n.loss_percent) / 100);
      parts.push(`${v.payload.quantity} × ${Number(n.rate)}${Number(n.loss_percent) ? ` + ${Number(n.loss_percent)}% потерь` : ""}`);
    }
    if (!norm) continue;
    const diff = ((u.payload.quantity - norm) / norm) * 100;
    const r2 = (x: number) => Math.round(x * 100) / 100;
    u.summary += ` По норме ${r2(norm)} (${parts.join("; ")}): ${diff <= 2 ? "в пределах." : `перерасход +${diff.toFixed(1)}%.`}`;
    if (diff > 2) {
      act(ev, "verifier", "flag_issue", `Перерасход к норме: ${u.title.replace(/^Расход: /, "").split(" — ")[0]} +${diff.toFixed(1)}%`,
        `Списано ${u.payload.quantity} при норме ${r2(norm)} на сообщённый объём — без объяснения не войдёт в акт списания; уточните у прораба.`,
        { severity: "warning", specialist_title: "Контролёр" });
    }
  }
  for (const a of ev.actions) {
    if (a.status !== "new") continue;
    if (!a.payload.object_id && ["report_volume", "record_usage"].includes(a.kind)) { a.status = "proposed"; a.summary += " Укажите объект."; continue; }
    const mode = (autonomyModes ?? {})[a.kind] ?? "auto";
    if (mode === "auto" && a.confidence >= 0.7) applyDemo(ev, a);
    else { a.status = "proposed"; a.auto = false; continue; }
    a.status = "applied";
  }
  ev.reply = [replyHead, ...ev.actions.map((a: Any) => `${a.status === "applied" ? "✓" : "?"} ${a.title}`)].join("\n");
  ai().events.unshift(ev);
  return ev;
}

function replaySample(id: string): Any {
  const sample = ai().samples.find((x) => x.id === id);
  if (!sample) throw new DemoError(404, "Пример не найден");
  const s = snap();
  const ev = structuredClone(sample.event);
  const oldId = ev.id;
  ev.id = uid();
  ev.created_at = now();
  if (s.ai!.files[oldId]) {
    s.ai!.files[ev.id] = s.ai!.files[oldId];
    ev.file_url = `/api/intake/${ev.id}/file`;
  }
  for (const a of ev.actions) {
    a.id = uid();
    a.event_id = ev.id;
    a.created_at = now();
    const docId = a.result?.document_id;
    const doc = docId && ai().sample_documents.find((d: Any) => d.id === docId);
    if (doc && a.status === "applied") a.result.document_id = addDocument(doc).id;
    if (a.status === "applied" && ["report_volume", "record_usage", "create_task"].includes(a.kind)) {
      applyDemo(ev, a);
    }
  }
  ai().events.unshift(ev);
  return ev;
}

function aiRoute(p: string, q: URLSearchParams, method: string, body: Any): Any {
  const parts = p.split("/").filter(Boolean);
  if (p === "/ai/feed") return feed(q);
  if (p === "/ai/samples") return ai().samples.map(({ id, title, hint }) => ({ id, title, hint }));
  if (p === "/ai/specialists") return specialists();
  if (p === "/ai/autonomy" && method === "PUT") {
    specialists();
    Object.assign(autonomyModes!, body.modes);
    return { autonomy: autonomyModes };
  }
  if (parts[1] === "events" && parts[2]) {
    const ev = ai().events.find((e: Any) => e.id === parts[2]);
    if (!ev) throw new DemoError(404, "Сообщение не найдено");
    return structuredClone(ev);
  }
  if (parts[1] === "actions" && parts[3] === "review") return review(parts[2], body);
  if (p === "/ai/actions/review-batch") {
    let done = 0;
    const errors: Any[] = [];
    for (const id of body.ids ?? []) {
      try { review(id, body); done += 1; } catch (e) { errors.push({ id, error: (e as Error).message }); }
    }
    return { done, errors };
  }
  if (p === "/ai/assemble-aosr") throw new DemoError(400, "В демо-версии АОСР собирается по сообщению о завершении работ (пример в ленте).");
  if (p === "/telegram/link") {
    throw new DemoError(400, "В демо-версии бот не подключён. На сервере задайте токен бота (BIS_TELEGRAM_BOT_TOKEN) — и каждый сотрудник привяжет свой Telegram здесь, одной командой.");
  }
  if (p === "/intake" && body instanceof FormData) {
    const sample = body.get("sample");
    if (sample) return replaySample(String(sample));
    const file = body.get("file") as File | null;
    const user = snap().get["/auth/me"]?.user;
    if (file && file.name === "voice.webm") {
      const ev: Any = { id: uid(), created_at: now(), source: "upload", kind: "voice", text: null, file_name: file.name, mime: file.type,
        file_url: null, object_id: null, object_name: null, sender: user?.full_name ?? null, status: "done", detected_type: null,
        dispatcher: null, transcript: null, excerpt: null, reply: null, needs_ocr: false, vision: null, error: null, document_id: null, actions: [] };
      act(ev, "hearing", "flag_issue", "Голосовое сохранено, но не распознано",
        "Распознавание речи не подключено (сервер STT). Подключите — и голосовые прорабов станут объёмами, расходом и поручениями.", { severity: "warning", specialist_title: "Слух" });
      ai().events.unshift(ev);
      return ev;
    }
    if (file) {
      const match = ai().samples.find((x) => x.event.file_name === file.name);
      if (match) return replaySample(match.id);
      return analyzeFile(file, String(body.get("text") ?? "").trim(), (body.get("object_id") as string) || null, user);
    }
    const text = String(body.get("text") ?? "").trim();
    if (!text) throw new DemoError(400, "Пришлите файл, фото, голосовое или текст");
    return understandLive(text, (body.get("object_id") as string) || null, user);
  }
  throw new DemoError(404, "Этот раздел недоступен в демо-версии");
}

// --- живой ИИ на странице (возможность sample: Claude вместо YandexGPT, который работает на сервере) ----

let samplePromise: Promise<Any> | null = null;
function getSample(): Promise<Any> {
  if (!samplePromise) {
    const c = (window as Any).claude;
    samplePromise = c?.use ? c.use("sample").catch(() => null) : Promise.resolve(null);
  }
  return samplePromise as Promise<Any>;
}
export async function liveAI(): Promise<boolean> {
  return !!(await getSample());
}

const LIVE_BY = "ИИ (в демо — Claude; на сервере — YandexGPT)";
const SAMPLE_OFF = new Set(["not_granted", "sampling_disabled", "not_declared", "capability_disabled", "capability_removed"]);

function catalog(objectId: string | null): { items: Any[]; text: string } {
  const s = snap();
  const items = s.lists["estimate-items"].filter((i) => !objectId || i.object_id === objectId);
  const text = "Позиции сметы (id | шифр | работа | ед.):\n" + items.map((i) => `${i.id} | ${i.code ?? ""} | ${i.name} | ${i.unit}`).join("\n")
    + "\n\nМатериалы справочника (id | наименование | ед.):\n" + s.lists.materials.map((m) => `${m.id} | ${m.name} | ${m.unit}`).join("\n");
  return { items, text };
}

async function understandLive(text: string, objectId: string | null, user: Any): Promise<Any> {
  const sample = await getSample();
  if (!sample) return understandRules(text, objectId, user);
  const ev = newEvent(text, objectId, user);
  const { items, text: cat } = catalog(ev.object_id);
  const prompt = `Ты — ИИ-диспетчер строительной компании. Разбери сообщение с объекта (часто разговорное, с ошибками распознавания речи).
${cat}

Ответь только JSON:
{"type": "foreman_report" | "question" | "other",
 "works": [{"item_id": "<id из списка позиций или null>", "description": "...", "quantity": number, "unit": "м3|м2|т|шт|п.м", "location": "захватка/оси/этаж или null"}],
 "materials": [{"material_id": "<id из справочника или null>", "name": "...", "quantity": number, "unit": "..."}],
 "problems": ["простои, нехватка, поломки, нарушения — каждое отдельной строкой"],
 "summary": "одна фраза: что понял"}
Правила: works — только выполненное (планы на завтра — не работы); израсходованный материал не дублируй в works;
item_id и material_id бери из списков, только если подходят по смыслу и единице; «кубов» = м3, «квадратов» = м2.

Сообщение: """${text.slice(0, 4000)}"""`;
  let r: Any;
  try {
    r = await sample.json(prompt, { modelTier: "quick" });
  } catch (e) {
    const code = (e as Any)?.code;
    const fallback = understandRules(text, objectId, user);
    fallback.dispatcher.by = SAMPLE_OFF.has(code) ? "правила (ИИ на странице не разрешён)" : "правила (ИИ не ответил)";
    return fallback;
  }
  ev.dispatcher = { type: r?.type === "foreman_report" ? "foreman_report" : "unknown",
    title: r?.type === "foreman_report" ? "Отчёт с площадки о выполненных работах, расходе, проблемах" : "Сообщение", confidence: 0.9, by: LIVE_BY };
  const s = snap();
  const today = new Date().toISOString().slice(0, 10);
  for (const w of Array.isArray(r?.works) ? r.works : []) {
    const item = items.find((i) => i.id === w.item_id);
    const qty = Number(w.quantity);
    if (!item || !qty) {
      act(ev, "dispatcher", "flag_issue", `Не сопоставлено со сметой: ${String(w.description ?? "").slice(0, 120)}`,
        "Нет подходящей позиции сметы — исправьте позицию в ленте, ИИ запомнит.", { severity: "warning" });
      continue;
    }
    act(ev, "dispatcher", "report_volume", `${item.name}: ${qty} ${item.unit}${w.location ? `, ${w.location}` : ""}`,
      `Из сообщения: «${String(w.description ?? "").slice(0, 200)}». Позиция сметы ${item.code ?? ""}.`
        + (item.requires_aosr ? " Скрытая работа — после завершения инженер ПТО соберёт АОСР." : ""),
      { payload: { object_id: ev.object_id ?? item.object_id, estimate_item_id: item.id, quantity: qty, location: w.location ?? null, date: today, note: w.description, source_text: w.description },
        status: "new", confidence: 0.9, evidence: [{ type: "estimate_item", id: item.id, label: `${item.code ?? ""} ${item.name}`.trim() }] });
  }
  for (const m of Array.isArray(r?.materials) ? r.materials : []) {
    const mat = s.lists.materials.find((x) => x.id === m.material_id);
    const qty = Number(m.quantity);
    if (!mat || !qty) {
      act(ev, "dispatcher", "flag_issue", `Материал не найден в справочнике: ${m.name ?? ""}`,
        `Расход ${qty || ""} ${m.unit ?? ""} не учтён — выберите материал, ИИ запомнит название.`, { severity: "warning" });
      continue;
    }
    act(ev, "dispatcher", "record_usage", `Расход: ${mat.name} — ${qty} ${mat.unit}`, "Из сообщения прораба.",
      { payload: { object_id: ev.object_id, material_id: mat.id, quantity: qty, date: today, note: "из отчёта с площадки", source_name: m.name }, status: "new", confidence: 0.88 });
  }
  for (const pr of Array.isArray(r?.problems) ? r.problems : []) {
    act(ev, "dispatcher", "create_task", `Проблема на объекте: ${String(pr).slice(0, 150)}`, "Поручение руководителю участка.",
      { payload: { title: String(pr), role: "manager", due_days: 1, priority: "high", object_id: ev.object_id }, status: "new", confidence: 0.85 });
  }
  if (!ev.actions.length) act(ev, "dispatcher", "info", r?.summary ? String(r.summary).slice(0, 200) : "Сообщение сохранено", "Работ, расхода или проблем не найдено.");
  return finalize(ev, `Понял: ${r?.summary ?? "сообщение с объекта"}`);
}

function blobToBase64(b: Blob): Promise<string> {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",")[1] ?? "");
    fr.onerror = () => rej(fr.error);
    fr.readAsDataURL(b);
  });
}

async function analyzeFile(file: File, caption: string, objectId: string | null, user: Any): Promise<Any> {
  const isImage = /^image\/(jpeg|png|webp|gif)$/i.test(file.type) || /\.(jpe?g|png|webp|gif)$/i.test(file.name);
  const isText = /\.(txt|csv|eml)$/i.test(file.name) || file.type.startsWith("text/");
  if (!isImage && !isText) {
    throw new DemoError(400, "В демо-версии ИИ читает фото (JPG, PNG) и текстовые файлы (TXT, CSV). PDF, DOCX и XLSX разбирает сервер.");
  }
  const sample = await getSample();
  if (!sample) throw new DemoError(400, "ИИ на этой странице недоступен — откройте её в Claude (на claude.ai). Пока можно попробовать примеры ниже.");
  const s = snap();
  const ev = newEvent(caption || null, objectId, user, {
    kind: isImage ? "photo" : "document", file_name: file.name, mime: file.type || null,
  });
  if (isImage) {
    s.ai!.files[ev.id] = { mime: file.type || "image/jpeg", data: await blobToBase64(file) };
    ev.file_url = `/api/intake/${ev.id}/file`;
  }
  const docText = isText ? (await file.text()).slice(0, 20000) : "";
  const prompt = `Ты — команда ИИ-специалистов строительной компании (диспетчер, чтец документов, зрение, контролёр).
${isImage ? "На изображении — фото с объекта или фото/скан документа." : "Ниже текст документа."}${caption ? ` Подпись отправителя: «${caption}».` : ""}
Определи тип и извлеки данные. Материалы справочника (id | наименование | ед.):
${s.lists.materials.map((m) => `${m.id} | ${m.name} | ${m.unit}`).join("\n")}

Ответь только JSON:
{"type": "upd" | "certificate" | "test_protocol" | "site_photo" | "letter" | "other",
 "title": "короткое название документа или что на фото",
 "supply": {"number": "...", "date": "ДД.ММ.ГГГГ", "supplier": "...", "items": [{"name": "...", "unit": "...", "quantity": number, "price": number|null, "material_id": "<id или null>"}], "certificates": ["упомянутые документы о качестве"]} | null,
 "quality": {"number": "...", "material": "...", "material_id": "<id или null>", "batch": "...", "valid_until": "ДД.ММ.ГГГГ|null"} | null,
 "protocol": {"number": "...", "structure": "...", "test": "...", "required": "...", "actual": "...", "compliant": true|false} | null,
 "photo": {"work_type": "...", "description": "что видно", "completion_percent": number|null, "defects": [{"description": "...", "severity": "low|medium|high"}], "safety_violations": ["люди без касок, нет ограждений…"]} | null,
 "letter": {"sender": "...", "subject": "...", "summary": "...", "requests": ["что просят сделать"], "due": "ДД.ММ.ГГГГ|null"} | null}
Не придумывай: чего не видно или нет в тексте — null или пустой список.${docText ? `\n\nТекст документа:\n"""${docText}"""` : ""}`;
  let r: Any;
  try {
    r = await sample.json(prompt, isImage ? { images: [file] } : {});
  } catch (e) {
    const code = (e as Any)?.code;
    throw new DemoError(400, SAMPLE_OFF.has(code) || code === "images_unavailable" ? "ИИ на этой странице не разрешён — разрешите его в окне Claude и попробуйте снова."
      : code === "image_rejected" ? "Это изображение не подходит — попробуйте другое фото (JPG/PNG до 20 МБ)." : "ИИ не ответил — попробуйте ещё раз.");
  }
  const TYPE_TITLES: Record<string, string> = { upd: "Накладная на поставку материалов (УПД, ТОРГ-12, ТТН)", certificate: "Документ о качестве (сертификат, паспорт, декларация)",
    test_protocol: "Протокол лабораторных испытаний", site_photo: "Фото с площадки", letter: "Письмо", other: "Документ" };
  const type = TYPE_TITLES[r?.type] ? r.type : "other";
  ev.detected_type = type;
  ev.dispatcher = { type, title: TYPE_TITLES[type], confidence: 0.9, by: LIVE_BY };
  ev.excerpt = docText.slice(0, 600) || null;
  const DOC_KINDS: Record<string, string> = { upd: "supply_doc", certificate: "certificate", test_protocol: "test_protocol", site_photo: "photo", letter: "incoming_letter", other: "other" };
  const doc = { id: uid(), kind: DOC_KINDS[type],
    title: String(r?.title ?? file.name).slice(0, 200), number: null, date: new Date().toISOString().slice(0, 10), status: "registered", source: "uploaded",
    file_name: file.name, issues: [], created_by: "agent:reader", created_at: now(), object_id: ev.object_id };
  s.documents.unshift(doc);
  act(ev, type === "site_photo" ? "vision" : "reader", "register_document", `Документ: ${doc.title}`, "Сохранён в реестре объекта.", { result: { document_id: doc.id } });

  if (type === "upd" && r.supply) {
    const sp = r.supply;
    const ref = `УПД № ${sp.number ?? "б/н"}${sp.date ? ` от ${sp.date}` : ""}`;
    for (const it of Array.isArray(sp.items) ? sp.items : []) {
      const mat = s.lists.materials.find((m) => m.id === it.material_id);
      act(ev, "reader", "create_receipt", `Приход: ${it.name} — ${it.quantity} ${it.unit}`,
        `${ref}${sp.supplier ? `, ${sp.supplier}` : ""}. ` + (mat ? `Материал в справочнике: «${mat.name}».` : "Такого материала нет в справочнике — будет добавлен.")
          + (it.price ? ` Цена ${Number(it.price).toLocaleString("ru-RU")} руб./${it.unit}.` : ""),
        { status: "proposed", auto: false, confidence: 0.88, payload: { object_id: ev.object_id, material_id: mat?.id ?? null,
          new_material: mat ? null : { name: it.name, unit: it.unit }, source_name: it.name, quantity: Number(it.quantity), price: it.price ? Number(it.price) : null,
          date: new Date().toISOString().slice(0, 10), document_ref: ref, quality_doc: (sp.certificates ?? []).join("; ") || null } });
    }
    if (!(sp.certificates ?? []).length) {
      act(ev, "reader", "create_task", `Запросить документы о качестве по ${ref}`, "В накладной нет ссылок на сертификаты/паспорта — без них материалы не войдут в АОСР.",
        { status: "new", payload: { title: `Запросить у поставщика документы о качестве по ${ref}`, role: "supply", due_days: 2, object_id: ev.object_id } });
    }
  } else if (type === "certificate" && r.quality) {
    const q = r.quality;
    const recs = s.lists["material-receipts"].filter((x) => !x.quality_doc && (q.material_id ? x.material_id === q.material_id : false));
    const label = `Документ о качестве № ${q.number ?? "б/н"}${q.batch ? `, партия ${q.batch}` : ""}`;
    if (recs.length) {
      act(ev, "reader", "link_certificate", `${label} привязан к поступлениям: ${q.material ?? ""}`, `${recs.length} поступл. без документа о качестве.`,
        { status: "new", confidence: 0.85, payload: { receipt_ids: recs.map((x) => x.id), text: label } });
    } else {
      act(ev, "reader", "info", `${label} сохранён: ${q.material ?? ""}`, "Поступлений этого материала без документа о качестве нет — привяжется, когда придёт накладная.");
    }
  } else if (type === "test_protocol" && r.protocol) {
    const pr = r.protocol;
    if (pr.compliant === false) {
      act(ev, "verifier", "flag_issue", `Несоответствие по протоколу: ${pr.structure ?? ""}`,
        `${pr.test ?? "Испытание"}: требуется ${pr.required ?? "—"}, фактически ${pr.actual ?? "—"}. Конструкцию нельзя предъявлять к приёмке до решения проектировщика.`,
        { severity: "critical", specialist_title: "Контролёр" });
      act(ev, "verifier", "create_task", `Решение по несоответствию: ${pr.structure ?? ""}`, "Согласовать с проектировщиком дополнительные испытания или усиление.",
        { status: "new", specialist_title: "Контролёр", payload: { title: `Несоответствие по протоколу № ${pr.number ?? "б/н"} — согласовать решение`, role: "pto", due_days: 3, priority: "high", object_id: ev.object_id } });
    } else {
      act(ev, "reader", "info", `Испытания подтверждают соответствие: ${pr.structure ?? ""}`, `${pr.test ?? ""}: ${pr.actual ?? ""} при требовании ${pr.required ?? "—"}.`);
    }
  } else if (type === "site_photo" && r.photo) {
    const ph = r.photo;
    ev.vision = ph;
    act(ev, "vision", "info", `На фото: ${ph.work_type ?? "площадка"}${ph.completion_percent != null ? `, готовность ~${ph.completion_percent}%` : ""}`, ph.description ?? "", { specialist_title: "Зрение" });
    for (const d of Array.isArray(ph.defects) ? ph.defects : []) {
      act(ev, "vision", "flag_issue", `Дефект: ${String(d.description).slice(0, 150)}`, "Обнаружено на фото — проверьте на месте.",
        { severity: d.severity === "high" ? "critical" : d.severity === "medium" ? "warning" : "info", specialist_title: "Зрение" });
    }
    for (const v of Array.isArray(ph.safety_violations) ? ph.safety_violations : []) {
      act(ev, "vision", "flag_issue", `Нарушение охраны труда: ${String(v).slice(0, 150)}`, "Обнаружено на фото с площадки.", { severity: "critical", specialist_title: "Зрение" });
      act(ev, "vision", "create_task", `Устранить нарушение ОТ: ${String(v).slice(0, 150)}`, "",
        { status: "new", specialist_title: "Зрение", payload: { title: `Устранить нарушение охраны труда: ${v}`, role: "foreman", due_days: 0, priority: "high", object_id: ev.object_id } });
    }
  } else if (type === "letter" && r.letter) {
    const l = r.letter;
    act(ev, "secretary", "process_letter", `Письмо: ${l.subject ?? doc.title}`, `${l.summary ?? ""}${l.due ? ` Ответ до ${l.due}.` : ""}`, { specialist_title: "Секретарь" });
    for (const q of Array.isArray(l.requests) ? l.requests : []) {
      act(ev, "secretary", "create_task", `Поручение по письму: ${String(q).slice(0, 150)}`, "",
        { status: "new", specialist_title: "Секретарь", payload: { title: String(q), role: "pto", due_days: 5, object_id: ev.object_id } });
    }
  } else {
    act(ev, "dispatcher", "info", `Сохранено: ${doc.title}`, "Тип не определён — уточните, что это, если нужна обработка.");
  }
  return finalize(ev, `Понял: ${TYPE_TITLES[type].toLowerCase()}.`);
}

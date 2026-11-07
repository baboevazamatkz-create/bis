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

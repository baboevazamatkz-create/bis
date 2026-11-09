// Клиент API BIS: JWT в localStorage, JSON и multipart-запросы, скачивание файлов с авторизацией.
// В демо-сборке (VITE_DEMO=1) запросы обслуживает mock на снимке данных настоящего backend.
import { mockApi, mockBlob } from "./demo/mock";

export const DEMO = import.meta.env.VITE_DEMO === "1";

const TOKEN_KEY = "bis_token";
let organizationScope = "";
export function setOrganizationScope(id: string) { organizationScope = id; }
export function activeProject(): string {
  try { return localStorage.getItem(`bis:project:${organizationScope}`) ?? ""; } catch { return ""; }
}
export function setActiveProject(id: string) {
  try { if (id) localStorage.setItem(`bis:project:${organizationScope}`, id); else localStorage.removeItem(`bis:project:${organizationScope}`); } catch { /* optional */ }
  window.dispatchEvent(new Event("bis:project"));
}
function projectHeader(headers: Headers) {
  const project = activeProject();
  if (project) headers.set("X-BIS-Project", project);
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* хранилище недоступно — токен живёт до перезагрузки */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function detail(data: unknown): string {
  if (data && typeof data === "object" && "detail" in data) {
    const d = (data as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((e) => `${(e.loc ?? []).join(".")}: ${e.msg}`).join("; ");
  }
  return "Ошибка запроса";
}

export async function api<T = unknown>(path: string, options: RequestInit & { json?: unknown } = {}): Promise<T> {
  if (DEMO) return mockApi<T>(path, options.method ?? "GET", options.json ?? options.body);
  const headers = new Headers(options.headers);
  if (!path.startsWith("/auth")) projectHeader(headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = options.body;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }
  const res = await fetch(`/api${path}`, { ...options, headers, body });
  if (res.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("bis:logout"));
  }
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, detail(data));
  return data as T;
}

export interface StreamEvent { type: "step" | "route" | "done" | "error"; label?: string; title?: string; agent?: string; reply?: unknown; detail?: string }

/** Ответ ассистента потоком: шаги работы приходят по мере выполнения, в конце — ответ целиком. */
export async function streamChat<T>(body: unknown, onEvent: (e: StreamEvent) => void): Promise<T> {
  if (DEMO) return api<T>("/chat", { method: "POST", json: body });
  const headers = new Headers({ "Content-Type": "application/json", Accept: "text/event-stream" });
  projectHeader(headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch("/api/chat/stream", { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok || !res.body) throw new ApiError(res.status, detail(await res.json().catch(() => ({}))));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const e = JSON.parse(line.slice(6)) as StreamEvent;
      onEvent(e);
      if (e.type === "done") return e.reply as T;
      if (e.type === "error") throw new ApiError(400, String(e.detail ?? "Ошибка"));
    }
  }
  throw new ApiError(500, "Ответ прервался — повторите вопрос");
}

export async function upload<T = unknown>(path: string, form: FormData): Promise<T> {
  return api<T>(path, { method: "POST", body: form });
}

/** Файл/предпросмотр с авторизацией как Blob (для изображений и аудио). */
export async function fetchBlob(path: string, init: RequestInit = {}): Promise<Blob> {
  if (DEMO) return mockBlob(path);
  const headers = new Headers(init.headers);
  projectHeader(headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`/api${path}`, { ...init, headers });
  if (!res.ok) throw new ApiError(res.status, "Не удалось получить файл");
  return res.blob();
}

export async function download(path: string, fallbackName = "document") {
  if (DEMO) {
    // в демо-странице скачивание недоступно — показываем предпросмотр документа
    window.dispatchEvent(new CustomEvent("bis:preview", { detail: path }));
    return;
  }
  const token = getToken();
  const res = await fetch(`/api${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!res.ok) throw new ApiError(res.status, "Не удалось скачать файл");
  const cd = res.headers.get("Content-Disposition") ?? "";
  const match = /filename\*=UTF-8''([^;]+)/.exec(cd);
  const name = match ? decodeURIComponent(match[1]) : fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Открыть документ: PDF, картинки и текст — в новой вкладке, остальное (Word, Excel, DWG) — скачать. */
export async function openFile(path: string, fallbackName = "document", page?: number | null) {
  if (DEMO) return download(path, fallbackName);
  const tab = window.open("", "_blank"); // окно открываем сразу по нажатию — иначе браузер его заблокирует
  try {
    const blob = await fetchBlob(path);
    if (tab && /^(application\/pdf|image\/|text\/)/.test(blob.type)) {
      // PDF открывается сразу на странице, где найден ответ
      tab.location.href = URL.createObjectURL(blob) + (page && blob.type === "application/pdf" ? `#page=${page}` : "");
      return;
    }
    tab?.close();
    await download(path, fallbackName);
  } catch (e) {
    tab?.close();
    throw e;
  }
}

// --- типы -------------------------------------------------------------------------------------

export interface User {
  id: string;
  email: string;
  full_name: string;
  position: string | null;
  role: string;
  is_platform_admin: boolean;
  organization_id: string;
}

export interface Citation {
  fact_snapshot_id?: string;
  object_id?: string;
  source_id?: string;
  chunk_id: string;
  citation: string;
  title: string;
  content: string;
  status: string;
  section_path: string | null;
  page?: number | null;
  document_id?: string | null;
}

export interface DocRef {
  id: string;
  kind: string;
  title: string;
  status: string;
  number: string | null;
}

export interface ChatReply {
  session_id: string;
  agent: string;
  agent_title: string;
  routed_by: string;
  text: string;
  citations: Citation[];
  documents: DocRef[];
  trace: { tool: string; error: boolean }[];
  provider: string | null;
  model: string | null;
}

export interface Issue {
  level: "error" | "warning";
  field: string;
  message: string;
}

export interface DocumentRow {
  id: string;
  kind: string;
  title: string;
  number: string | null;
  date: string | null;
  status: string;
  source: string;
  file_name: string | null;
  issues: Issue[];
  created_by: string | null;
  created_at: string;
  reject_reason: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data?: Record<string, any>;
}

export interface Source {
  id: string;
  kind: string;
  title: string;
  designation: string | null;
  status: string;
  chunk_count: number;
  is_global: boolean;
  file_name: string | null;
  meta: Record<string, unknown>;
}

export const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  on_approval: "На согласовании",
  approved: "Утверждён",
  rejected: "Отклонён",
  signed: "Подписан",
  registered: "Зарегистрирован",
  closed: "Закрыт",
  active: "Действует",
  cancelled: "Отменён",
  replaced: "Заменён",
  unknown: "Не проверен",
};

// --- ИИ-конвейер ---------------------------------------------------------------------------------

export interface Evidence {
  type: string;
  label: string | null;
  id?: string;
  event_id?: string;
}

export interface AIAction {
  id: string;
  event_id: string | null;
  specialist: string;
  specialist_title: string;
  kind: string;
  kind_title: string;
  title: string;
  summary: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: Record<string, any>;
  evidence: Evidence[];
  confidence: number;
  severity: "info" | "warning" | "critical";
  status: "proposed" | "applied" | "rejected" | "failed" | "new";
  auto: boolean;
  corrected: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  result: Record<string, any>;
  object_id: string | null;
  reviewed_at: string | null;
  reason: string | null;
  created_at: string | null;
  editable: string[];
}

export interface IntakeEvent {
  id: string;
  created_at: string | null;
  source: string;
  kind: "photo" | "voice" | "document" | "text";
  text: string | null;
  file_name: string | null;
  mime: string | null;
  file_url: string | null;
  object_id: string | null;
  object_name: string | null;
  sender: string | null;
  status: string;
  detected_type: string | null;
  dispatcher: { type: string; title: string; confidence: number; by: string } | null;
  transcript: string | null;
  excerpt: string | null;
  reply: string | null;
  needs_ocr: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  vision: Record<string, any> | null;
  error: string | null;
  document_id: string | null;
  actions: AIAction[];
}

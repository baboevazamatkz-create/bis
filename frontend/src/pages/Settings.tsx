import { useEffect, useState } from "react";
import { api, upload, type User } from "../api";
import { Hint } from "../components";

interface Org {
  name: string;
  short_name: string | null;
  inn: string | null;
  kpp: string | null;
  ogrn: string | null;
  address: string | null;
  phone: string | null;
  email: string | null;
  sro: string | null;
  settings: {
    default_signer?: { position?: string; name?: string };
    letter_number_format?: string;
    mailbox?: { enabled?: boolean; host?: string; port?: number; username?: string; password_env?: string; folder?: string };
    routing?: Record<string, string>;
  };
}

interface Memory {
  id: string;
  kind: string;
  scope: string;
  content: string;
  source: string | null;
  is_active: boolean;
}

const ORG_FIELDS: [keyof Org, string][] = [
  ["name", "Полное наименование"], ["short_name", "Краткое наименование"], ["inn", "ИНН"], ["kpp", "КПП"],
  ["ogrn", "ОГРН"], ["address", "Адрес"], ["phone", "Телефон"], ["email", "Email"], ["sro", "СРО (наименование, ОГРН, ИНН)"],
];

export default function Settings() {
  const [org, setOrg] = useState<Org | null>(null);
  const [memory, setMemory] = useState<Memory[]>([]);
  const [rule, setRule] = useState("");
  const [ruleScope, setRuleScope] = useState("*");
  const [ruleKind, setRuleKind] = useState("rule");
  const [templates, setTemplates] = useState<{ id: string; kind: string; name: string; is_default: boolean }[]>([]);
  const [tplFile, setTplFile] = useState<File | null>(null);
  const [tplKind, setTplKind] = useState("letter");
  const [users, setUsers] = useState<User[]>([]);
  const [newUser, setNewUser] = useState({ email: "", full_name: "", position: "", role: "pto", password: "" });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const report = (p: Promise<unknown>, ok: string) =>
    p.then(() => setMsg({ ok: true, text: ok })).catch((e) => setMsg({ ok: false, text: e.message }));

  const loadAll = () => {
    api<Org>("/org").then(setOrg);
    api<Memory[]>("/memory").then(setMemory);
    api<typeof templates>("/templates").then(setTemplates);
    api<User[]>("/users").then(setUsers).catch(() => setUsers([]));
  };
  useEffect(loadAll, []);

  if (!org) return <div className="muted">Загрузка…</div>;
  const signer = org.settings?.default_signer ?? {};

  return (
    <>
      <h1>Настройки организации</h1>
      {msg && <div className={`alert ${msg.ok ? "ok" : "error"}`}>{msg.text}</div>}
      <div className="panel">
        <h2>Реквизиты (для бланков и ИД)</h2>
        <div className="row">
          {ORG_FIELDS.map(([k, label]) => (
            <div className="field" key={k}>
              <label>{label}</label>
              <input value={(org[k] as string) ?? ""} onChange={(e) => setOrg({ ...org, [k]: e.target.value })} />
            </div>
          ))}
        </div>
        <div className="row">
          <div className="field">
            <label>Подписант писем — должность</label>
            <input value={signer.position ?? ""} onChange={(e) =>
              setOrg({ ...org, settings: { ...org.settings, default_signer: { ...signer, position: e.target.value } } })} />
          </div>
          <div className="field">
            <label>Подписант — И.О. Фамилия</label>
            <input value={signer.name ?? ""} onChange={(e) =>
              setOrg({ ...org, settings: { ...org.settings, default_signer: { ...signer, name: e.target.value } } })} />
          </div>
          <div className="field">
            <label>Формат исх. номера ({"{seq}"}, {"{yy}"}, {"{yyyy}"})</label>
            <input value={org.settings?.letter_number_format ?? "{seq}/{yy}"} onChange={(e) =>
              setOrg({ ...org, settings: { ...org.settings, letter_number_format: e.target.value } })} />
          </div>
        </div>
        <button className="btn primary" onClick={() => {
          const { name, short_name, inn, kpp, ogrn, address, phone, email, sro, settings } = org;
          report(api("/org", { method: "PATCH", json: { name, short_name, inn, kpp, ogrn, address, phone, email, sro, settings } }), "Сохранено");
        }}>
          Сохранить
        </button>
      </div>

      <TelegramPanel />

      <div className="panel">
        <h2>Входящая почта (IMAP) <Hint>Письма и вложения автоматически регистрируются, попадают в базу знаний и разбираются агентом
          «Секретарь» (поручения, сроки ответа). Пароль хранится в переменной окружения сервера.</Hint></h2>
        <div className="row">
          <div className="field" style={{ flex: "0 0 auto" }}>
            <label><input type="checkbox" style={{ width: "auto" }} checked={!!org.settings?.mailbox?.enabled}
              onChange={(e) => setOrg({ ...org, settings: { ...org.settings, mailbox: { ...org.settings?.mailbox, enabled: e.target.checked } } })} /> Включено</label>
          </div>
          {([["host", "Сервер IMAP"], ["port", "Порт"], ["username", "Логин"], ["password_env", "Переменная с паролем"], ["folder", "Папка"]] as const).map(([k, l]) => (
            <div className="field" key={k}>
              <label>{l}</label>
              <input value={String(org.settings?.mailbox?.[k] ?? (k === "port" ? 993 : k === "folder" ? "INBOX" : ""))}
                onChange={(e) => setOrg({ ...org, settings: { ...org.settings, mailbox: { ...org.settings?.mailbox, [k]: k === "port" ? Number(e.target.value) : e.target.value } } })} />
            </div>
          ))}
        </div>
        <h2 style={{ marginTop: 12 }}>Маршрутизация поручений</h2>
        <p className="muted small">Кому направлять поручения из входящих писем по направлению (email пользователя). Пусто — первому пользователю с этой ролью.</p>
        <div className="row">
          {([["pto", "ПТО / ИД"], ["lawyer", "Договоры, претензии"], ["supply", "Материалы, поставки"], ["foreman", "Работы на площадке"], ["manager", "Оплата, прочее"], ["secretary", "Ответы на письма"]] as const).map(([k, l]) => (
            <div className="field" key={k}>
              <label>{l}</label>
              <input value={org.settings?.routing?.[k] ?? ""} placeholder="email"
                onChange={(e) => setOrg({ ...org, settings: { ...org.settings, routing: { ...org.settings?.routing, [k]: e.target.value } } })} />
            </div>
          ))}
        </div>
        <button className="btn primary" onClick={() => {
          const { name, short_name, inn, kpp, ogrn, address, phone, email, sro, settings } = org;
          report(api("/org", { method: "PATCH", json: { name, short_name, inn, kpp, ogrn, address, phone, email, sro, settings } }), "Сохранено");
        }}>Сохранить</button>
      </div>

      <div className="panel">
        <h2>Правила и память организации <Hint>Правила добавляются в инструкции агентов. Замечания при отклонении документов попадают сюда автоматически — так система
          подстраивается под компанию. Термины компании для поиска: тип «term», формат «термин = синоним1; синоним2».</Hint></h2>
        <div className="row">
          <div className="field">
            <input value={rule} placeholder="Например: в письмах заказчику всегда указывать номер договора" onChange={(e) => setRule(e.target.value)} />
          </div>
          <div className="field" style={{ flex: "0 0 140px" }}>
            <select value={ruleKind} onChange={(e) => setRuleKind(e.target.value)}>
              <option value="rule">Правило</option>
              <option value="style">Стиль</option>
              <option value="fact">Факт</option>
              <option value="term">Термин</option>
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 180px" }}>
            <select value={ruleScope} onChange={(e) => setRuleScope(e.target.value)}>
              <option value="*">Все агенты</option>
              <option value="secretary">Секретарь</option>
              <option value="pto">ПТО</option>
              <option value="norms">Нормоконтроль</option>
              <option value="contracts">Договорной</option>
              <option value="works">Контроль работ</option>
              <option value="materials">Материалы</option>
              <option value="closing">Закрытие работ</option>
              <option value="project">Анализ ПД</option>
              <option value="assistant">Помощник</option>
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!rule.trim()} onClick={() =>
              report(api("/memory", { method: "POST", json: { content: rule, scope: ruleScope, kind: ruleKind } }).then(() => {
                setRule("");
                loadAll();
              }), "Правило добавлено")}>
              Добавить
            </button>
          </div>
        </div>
        <table>
          <tbody>
            {memory.map((m) => (
              <tr key={m.id}>
                <td>{m.content}</td>
                <td className="small muted">{m.scope === "*" ? "все агенты" : m.scope} · {m.kind}</td>
                <td>
                  <label className="small">
                    <input type="checkbox" style={{ width: "auto" }} checked={m.is_active} onChange={(e) =>
                      api(`/memory/${m.id}`, { method: "PATCH", json: { is_active: e.target.checked } }).then(loadAll)} />{" "}
                    активно
                  </label>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Шаблоны документов <Hint>DOCX с полями в фигурных скобках, например {"{{ number }}"}, {"{{ date_text }}"}, {"{{ subject }}"}, {"{{ developer_text }}"}.
          Шаблон по умолчанию заменяет встроенную форму.</Hint></h2>
        <div className="row">
          <div className="field">
            <input type="file" accept=".docx" onChange={(e) => setTplFile(e.target.files?.[0] ?? null)} />
          </div>
          <div className="field" style={{ flex: "0 0 200px" }}>
            <select value={tplKind} onChange={(e) => setTplKind(e.target.value)}>
              <option value="letter">Исходящее письмо</option>
              <option value="aosr">АОСР</option>
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 auto" }}>
            <button className="btn primary" disabled={!tplFile} onClick={() => {
              const form = new FormData();
              form.append("file", tplFile!);
              form.append("kind", tplKind);
              form.append("name", tplFile!.name);
              report(upload("/templates", form).then(loadAll), "Шаблон загружен");
            }}>
              Загрузить
            </button>
          </div>
        </div>
        <ul>
          {templates.map((t) => (
            <li key={t.id}>
              {t.name} — {t.kind} {t.is_default && <span className="badge active">по умолчанию</span>}{" "}
              <button className="btn small" onClick={() => api(`/templates/${t.id}`, { method: "DELETE" }).then(loadAll)}>
                Удалить
              </button>
            </li>
          ))}
        </ul>
      </div>

      {users.length > 0 && (
        <div className="panel">
          <h2>Пользователи</h2>
          <table>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>{u.full_name}</td>
                  <td>{u.email}</td>
                  <td>{u.position}</td>
                  <td>{u.role}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h2 style={{ marginTop: 16 }}>Новый пользователь</h2>
          <div className="row">
            {(["full_name", "email", "position", "password"] as const).map((k) => (
              <div className="field" key={k}>
                <label>{{ full_name: "ФИО", email: "Email", position: "Должность", password: "Пароль (от 8 символов)" }[k]}</label>
                <input type={k === "password" ? "password" : "text"} value={newUser[k]} onChange={(e) => setNewUser({ ...newUser, [k]: e.target.value })} />
              </div>
            ))}
            <div className="field">
              <label>Роль</label>
              <select value={newUser.role} onChange={(e) => setNewUser({ ...newUser, role: e.target.value })}>
                {["admin", "manager", "pto", "foreman", "supply", "lawyer", "secretary", "viewer"].map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
            </div>
          </div>
          <button className="btn primary" onClick={() =>
            report(api("/users", { method: "POST", json: newUser }).then(() => {
              setNewUser({ email: "", full_name: "", position: "", role: "pto", password: "" });
              loadAll();
            }), "Пользователь создан")}>
            Создать
          </button>
        </div>
      )}
      {users.length > 0 && <ResetPanel />}
    </>
  );
}

/** Очистка данных организации: чистый старт пилота. Только администратор, с подтверждением словом. */
function ResetPanel() {
  const [open, setOpen] = useState(false);
  const [word, setWord] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  async function reset() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api<{ rows: number; files: number }>("/org/reset", { method: "POST", json: { confirm: word } });
      setMsg(`Готово: удалено записей ${r.rows}, файлов ${r.files}. Можно загружать архив заново.`);
      setOpen(false);
      setWord("");
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="panel danger-zone">
      <h2>Очистка данных <Hint>Удаляются документы и файлы, база знаний организации, карточки «Эрудита», объекты, договоры,
        сметы, учёт, сообщения с площадки, поручения, уведомления и чаты. Остаются реквизиты, пользователи, общие нормативы
        и ключи. Отменить нельзя.</Hint></h2>
      {!open && <button className="btn danger" onClick={() => setOpen(true)}>Очистить все данные организации…</button>}
      {open && (
        <div className="row" style={{ alignItems: "center" }}>
          <input value={word} onChange={(e) => setWord(e.target.value)} placeholder="Напишите УДАЛИТЬ" style={{ maxWidth: 220 }} autoFocus />
          <button className="btn danger" disabled={busy || word.trim().toUpperCase() !== "УДАЛИТЬ"} onClick={() => void reset()}>
            {busy ? "Удаляю…" : "Удалить безвозвратно"}
          </button>
          <button className="btn" onClick={() => { setOpen(false); setWord(""); }}>Отмена</button>
        </div>
      )}
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}

function TelegramPanel() {
  const [link, setLink] = useState<{ command: string; deep_link: string | null; expires_in: number; linked: boolean } | null>(null);
  const [error, setError] = useState("");
  async function getLink() {
    setError("");
    try {
      setLink(await api("/telegram/link", { method: "POST" }));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="panel">
      <h2>Telegram: сообщения с площадки <Hint>Прорабы, мастера и снабженцы отправляют боту фото, голосовые, УПД, сертификаты и текст — ИИ разбирает, вносит данные
        и отвечает, что сделал. Предложения ИИ можно принять кнопкой прямо в чате. Каждый подключает свой аккаунт.</Hint></h2>
      <button className="btn" onClick={() => void getLink()}>Подключить мой Telegram</button>
      {error && <div className="alert error" style={{ marginTop: 10 }}>{error}</div>}
      {link && (
        <div className="note" style={{ marginTop: 10 }}>
          {link.deep_link ? <>Откройте ссылку: <a href={link.deep_link} target="_blank" rel="noreferrer">{link.deep_link}</a><br /></> : null}
          Или отправьте боту команду: <code>{link.command}</code>
          <div className="small muted">Код действует {Math.round(link.expires_in / 60)} мин.{link.linked ? " Сейчас аккаунт уже привязан — новая привязка заменит старую." : ""}</div>
        </div>
      )}
    </div>
  );
}

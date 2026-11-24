import { useEffect, useRef, useState } from 'react';
import { api, openFile } from '../../api';
import './ProjectStructure.css';

type Fields = Record<string, string | null | undefined>;
type Source = { document_id: string; title: string; level: 'organization' | 'project' };
type Role = 'organization' | 'developer' | 'contractor' | 'subcontractor' | 'designer' | 'supplier' | 'other';
type Participant = {
  key: string;
  role: Role;
  own?: boolean;
  counterparty_id?: string | null;
  name?: string | null;
  department?: string | null;
  sources?: Source[];
  contracts?: { id: string; number: string; subject?: string | null }[];
};
type Person = Fields & { appointment_id?: string; person_id?: string; party_key?: string | null };
type PersonRow = { fields: Person; sources?: Source[] };
type PersonPayload = Record<string, unknown> & { sources?: Source[] };
type Structure = {
  revision: string;
  project: Fields;
  project_sources: Source[];
  parties: Participant[];
  people: PersonPayload[];
  warnings: string[];
  confirmed_at: string | null;
  can_edit: boolean;
  can_edit_organization: boolean;
};
type Field = [string, string, number?, 'date'?];

const PATH = '/workspace/project-structure';
const ROLES: [Role, string][] = [
  ['developer', 'Заказчик'], ['contractor', 'Подрядчик'], ['subcontractor', 'Субподрядчики'],
  ['designer', 'Проектировщики'], ['supplier', 'Поставщики'], ['other', 'Другие участники'],
];
const PROJECT_FIELDS: Field[] = [['name', 'Название', 1000], ['code', 'Шифр', 100],
  ['address', 'Адрес объекта', 2000], ['start_date', 'Начало', undefined, 'date'], ['end_date', 'Окончание', undefined, 'date']];
const PARTY_FIELDS: Field[] = [['name', 'Наименование', 500], ['department', 'Отдел / подразделение', 300],
  ['inn', 'ИНН', 12], ['kpp', 'КПП', 9], ['ogrn', 'ОГРН / ОГРНИП', 15], ['address', 'Юридический адрес', 2000],
  ['phone', 'Телефон', 100], ['email', 'Электронная почта', 200], ['sro', 'СРО', 2000],
  ['head_name', 'ФИО подписанта', 300], ['head_position', 'Должность подписанта', 300]];
const PERSON_FIELDS: Field[] = [['full_name', 'ФИО', 300], ['position', 'Должность', 300], ['department', 'Отдел', 300],
  ['phone', 'Телефон', 100], ['email', 'Электронная почта', 200], ['nrs_id', 'Номер НРС', 50], ['order_number', 'Номер приказа', 100],
  ['order_date', 'Дата приказа', undefined, 'date'], ['valid_from', 'Полномочия с', undefined, 'date'], ['valid_to', 'Полномочия до', undefined, 'date']];
const PERSON_ROLES: [string, string][] = [['developer_control', 'Строительный контроль заказчика'],
  ['builder', 'Представитель подрядчика'], ['builder_control', 'Строительный контроль подрядчика'],
  ['designer', 'Представитель проектировщика'], ['performer', 'Представитель исполнителя работ'], ['responsible', 'Ответственное лицо']];
const ROLE_LABELS = Object.fromEntries(ROLES);

function FieldGrid({ fields, values, disabled, change }: {
  fields: Field[]; values: Fields; disabled: boolean; change: (name: string, value: string | null) => void;
}) {
  return <div className="structure-fields">{fields.map(([name, label, maxLength, type]) =>
    <label className="field" key={name}>{label}<input type={type ?? 'text'} maxLength={maxLength}
      disabled={disabled} value={values[name] ?? ''} onChange={e => change(name, e.target.value || null)} /></label>,
  )}</div>;
}

function Sources({ sources = [], onError }: { sources?: Source[]; onError: (message: string) => void }) {
  if (!sources.length) return null;
  return <ul className="structure-sources">{sources.map(source => <li key={source.document_id}>
    <button type="button" className="btn small" onClick={() => void openFile('/documents/' + source.document_id + '/file', source.title).catch(
      (error: Error) => onError(error.message),
    )}>{source.title}</button>
    <span className="small muted">{source.level === 'organization' ? 'База организации' : 'База проекта'}</span>
  </li>)}</ul>;
}

function readPeople(data: Structure): PersonRow[] {
  return data.people.map(({ sources, ...fields }) => ({ fields: fields as Person, sources }));
}

export default function ProjectStructure({ projectId }: { projectId: string }) {
  const nextParty = useRef(0);
  const [data, setData] = useState<Structure | null>(null);
  const [people, setPeople] = useState<PersonRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  function receive(result: Structure) {
    setData(result);
    setPeople(readPeople(result));
    setDirty(false);
  }

  async function load(signal?: AbortSignal) {
    setLoading(true);
    setError('');
    setNotice('');
    try {
      receive(await api<Structure>(PATH, { signal }));
    } catch (error) {
      if (!signal?.aborted) setError((error as Error).message);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [projectId]);

  function editParty(key: string, name: string, value: string | boolean | null) {
    setDirty(true);
    setData(old => old && { ...old, parties: old.parties.map(party => party.key === key ? { ...party, [name]: value } : party) });
  }

  function addParty(role: Role) {
    setDirty(true);
    const key = `new:${Date.now()}:${nextParty.current++}`;
    setData(old => old && { ...old, parties: [...old.parties, { key, role }] });
  }

  function editPerson(index: number, name: string, value: string | null) {
    setDirty(true);
    setPeople(old => old.map((row, i) => i === index ? { ...row, fields: { ...row.fields, [name]: value } } : row));
  }

  async function confirm() {
    if (!data || busy) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api<Structure>(PATH, {
        method: 'PUT',
        json: { revision: data.revision, project: data.project, parties: data.parties, people: people.map(row => row.fields) },
      });
      receive(result);
      window.dispatchEvent(new Event('bis:objects'));
      setNotice('Структура подтверждена. Карточки BIS обновлены.');
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const canEdit = Boolean(data?.can_edit && !busy && !loading);
  const today = data?.confirmed_at ? new Date(data.confirmed_at).toLocaleString('ru-RU') : null;
  const own = data?.parties.find(p => p.role === 'organization' && p.own);

  function partyCard(party: Participant) {
    const assigned = people.filter(row => row.fields.party_key === party.key && row.fields.full_name);
    const locked = !canEdit || !!party.own && (party.role !== 'organization' || !data?.can_edit_organization);
    return <details className="structure-card" key={party.key} open={party.key.startsWith('new:') || undefined}>
      <summary><strong>{party.name || 'Карточка участника'}</strong>
        {party.department && <span className="small muted">{party.department}</span>}
        {!!assigned.length && <span className="small muted">{assigned.map(row => row.fields.full_name).join(', ')}</span>}
      </summary>
      <FieldGrid fields={PARTY_FIELDS} values={party as unknown as Fields} disabled={locked}
        change={(name, value) => editParty(party.key, name, value)} />
      {!party.counterparty_id && !party.sources?.length && party.role !== 'organization' && canEdit && <label className="structure-own">
        <input type="checkbox" checked={Boolean(party.own)} onChange={e => editParty(party.key, 'own', e.target.checked)} /> Наша организация
      </label>}
      {party.role !== 'organization' && !party.contracts?.length && canEdit && <label className="field structure-role">Роль в проекте
        <select value={party.role} onChange={e => editParty(party.key, 'role', e.target.value)}>
          {ROLES.map(([role, label]) => <option value={role} key={role}>{label}</option>)}
        </select>
      </label>}
      {!!party.contracts?.length && <ul className="structure-contracts">
        {party.contracts.map(contract => <li key={contract.id}>Договор № {contract.number}{contract.subject && ` · ${contract.subject}`}</li>)}
      </ul>}
      <Sources sources={party.sources} onError={setError} />
      {canEdit && !party.contracts?.length && party.role !== 'organization' &&
        <button type="button" className="btn small" onClick={() => {
          setDirty(true);
          setData(old => old && { ...old, parties: old.parties.filter(item => item.key !== party.key) });
          setPeople(old => old.map(row => row.fields.party_key === party.key
            ? { ...row, fields: { ...row.fields, party_key: null } } : row));
        }}>Убрать из схемы</button>}
    </details>;
  }

  return <section className="panel project-structure" aria-labelledby="project-structure-title">
    <div className="row section-list-header">
      <div><h2 id="project-structure-title">Структура проекта</h2>
        {data && <p className="small muted">{dirty ? 'Есть изменения для подтверждения' : today ? `Последнее подтверждение ${today}` : 'Ожидает подтверждения'}</p>}
      </div>
      <div className="row">
        <button type="button" className="btn" disabled={loading || busy} onClick={() => {
          if (!dirty || window.confirm('Загрузить данные заново и сбросить текущие правки?')) void load();
        }}>Обновить из баз</button>
        {data?.can_edit && <button type="button" className="btn primary" disabled={!canEdit} onClick={() => void confirm()}>
          {busy ? 'Подтверждаю…' : 'Подтвердить структуру'}
        </button>}
      </div>
    </div>
    {loading && <p role="status" className="small muted">Загружаю структуру проекта…</p>}
    {notice && <p role="status" className="note">{notice}</p>}
    {error && <p role="alert" className="alert error">{error}</p>}
    {data && <>
      {!!data.warnings.length && <ul className="note structure-warnings">{data.warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>}
      <div className="structure-diagram">
        <details className="structure-root structure-card">
          <summary><span className="small muted">Объект</span><strong>{data.project.name || 'Проект'}</strong>
            <span className="small muted">{[data.project.code, data.project.address].filter(Boolean).join(' · ')}</span>
          </summary>
          <FieldGrid fields={PROJECT_FIELDS} values={data.project} disabled={!canEdit} change={(name, value) => {
            setDirty(true);
            setData(old => old && { ...old, project: { ...old.project, [name]: value } });
          }} />
          <Sources sources={data.project_sources} onError={setError} />
        </details>
        <ul className="structure-branches">
          {ROLES.map(([role, label]) => <li className="structure-branch" key={role}>
            <h3>{label}</h3>
            {data.parties.filter(party => party.role === role).map(partyCard)}
            {canEdit && <button type="button" className="btn small" aria-label={`Добавить участника в группу «${label}»`}
              onClick={() => addParty(role)}>Добавить</button>}
          </li>)}
        </ul>
      </div>
      {own && <div className="structure-section"><h3>Наша организация</h3>{partyCard(own)}</div>}
      <div className="structure-section">
        <div className="row section-list-header"><h3>Отделы и ответственные лица</h3>
          {canEdit && <button type="button" className="btn small" onClick={() => {
            setDirty(true);
            setPeople(old => [...old, { fields: {} }]);
          }}>Добавить ответственное лицо</button>}
        </div>
        {people.map(({ fields, sources }, index) => <details className="structure-card" key={fields.appointment_id ?? fields.person_id ?? index}>
          <summary><strong>{fields.full_name || 'Карточка ответственного лица'}</strong>
            <span className="small muted">{[fields.department, fields.position].filter(Boolean).join(' · ')}</span>
          </summary>
          <FieldGrid fields={PERSON_FIELDS} values={fields} disabled={!canEdit} change={(name, value) => editPerson(index, name, value)} />
          <div className="structure-fields">
            <label className="field">Организация<select aria-label="Организация ответственного" disabled={!canEdit} value={fields.party_key ?? ''}
              onChange={e => editPerson(index, 'party_key', e.target.value || null)}>
              <option value="">—</option>
              {data.parties.map(party => <option key={party.key} value={party.key}>{party.name || ROLE_LABELS[party.role] || 'Наша организация'}</option>)}
            </select></label>
            <label className="field">Роль представителя<select aria-label="Роль представителя" disabled={!canEdit} value={fields.role ?? ''}
              onChange={e => editPerson(index, 'role', e.target.value || null)}>
              <option value="">—</option>
              {fields.role && !PERSON_ROLES.some(([role]) => role === fields.role) && <option value={fields.role}>{fields.role}</option>}
              {PERSON_ROLES.map(([role, label]) => <option value={role} key={role}>{label}</option>)}
            </select></label>
          </div>
          {!fields.appointment_id && fields.full_name && !fields.party_key && <p className="small muted">
            Выберите организацию, чтобы назначение использовалось в документах.
          </p>}
          <Sources sources={sources} onError={setError} />
          {canEdit && !fields.appointment_id && !fields.person_id && <button type="button" className="btn small" onClick={() => {
            setDirty(true);
            setPeople(old => old.filter((_, i) => i !== index));
          }}>Убрать строку</button>}
        </details>)}
      </div>
    </>}
  </section>;
}

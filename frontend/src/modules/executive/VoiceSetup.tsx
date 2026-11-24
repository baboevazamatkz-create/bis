import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api } from '../../api';

interface Props {
  open: boolean;
  canWrite: boolean;
  available?: boolean;
  configured?: boolean;
  usesOpenAI: boolean;
  onSave: (configured: boolean) => void;
}

export default function VoiceSetup({ open, canWrite, available, configured, usesOpenAI, onSave }: Props) {
  const panel = useRef<HTMLDetailsElement>(null);
  const [provider, setProvider] = useState<'openai' | 'yandex'>('openai');
  const [key, setKey] = useState('');
  const [folder, setFolder] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (open && panel.current) {
      panel.current.open = true;
      panel.current.scrollIntoView();
    }
  }, [open]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await api<{ stt: boolean; message: string }>('/voice/setup', {
        method: 'POST',
        json: { provider, api_key: key.trim(), ...(provider === 'yandex' ? { folder_id: folder.trim() } : {}) },
      });
      setKey('');
      setMessage(result.message);
      onSave(result.stt);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <details ref={panel} className="panel">
    <summary>Распознавание речи</summary>
    <p>{configured === undefined ? 'Проверяем настройки голоса…' :
      configured ? 'Распознавание речи настроено.' : 'Распознавание речи не подключено.'}</p>
    {usesOpenAI && <p className="small muted">Для Whisper можно использовать сохранённый ключ OpenAI. Отдельные настройки голоса имеют приоритет.</p>}
    {configured === false && !canWrite && <p>Подключить распознавание может администратор BIS.</p>}
    {configured === false && canWrite && available === false &&
      <p>Для этой установки администратор задаёт провайдера распознавания на сервере.</p>}
    {canWrite && available && (!usesOpenAI || !configured) && <form className="connection-editor" onSubmit={(event) => void save(event)}>
      <label className="field">Провайдер распознавания
        <select value={provider} disabled={busy} onChange={(event) => {
          setProvider(event.target.value as 'openai' | 'yandex');
          setKey('');
          setError('');
          setMessage('');
        }}>
          <option value="openai">OpenAI Whisper</option>
          <option value="yandex">Yandex SpeechKit</option>
        </select>
      </label>
      {provider === 'yandex' && <label className="field">Folder ID
        <input required maxLength={100} value={folder} disabled={busy} onChange={(event) => setFolder(event.target.value)} />
      </label>}
      <label className="field">API-ключ для распознавания
        <input type="password" autoComplete="new-password" required maxLength={514} value={key}
          disabled={busy} onChange={(event) => setKey(event.target.value)} />
      </label>
      <p className="small muted">Аудиозаписи для распознавания отправляются выбранному провайдеру.</p>
      <button className="btn primary" disabled={busy || !key.trim() || (provider === 'yandex' && !folder.trim())}>
        {busy ? 'Сохраняю…' : 'Сохранить настройки голоса'}
      </button>
    </form>}
    {error && <p role="alert" className="text-bad">{error}</p>}
    {message && <p role="status">{message}</p>}
  </details>;
}

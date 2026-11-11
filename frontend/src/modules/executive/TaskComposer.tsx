import { useState } from 'react';
import { VoiceButton } from '../../components';
import { askAssistant } from './assistantTask';

export default function TaskComposer({ placeholder, documentId, title }: { placeholder: string; documentId?: string; title?: string }) {
  const [text, setText] = useState('');
  return <div className="task-composer panel">
    {title && <p className="small muted">По документу: {title}</p>}
    <div className="row"><textarea aria-label="Задание помощнику" rows={2} placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)} />
      <VoiceButton onText={setText} label="Надиктовать задание" />
      <button className="btn primary" disabled={!text.trim()} onClick={() => askAssistant(text, documentId, title)}>Поручить Синапсу</button>
    </div>
  </div>;
}

import { navigate } from '../navigation';

export interface AssistantTask { text: string; document_id?: string; title?: string }
const KEY = 'bis:assistant-task';
export function askAssistant(text: string, document_id?: string, title?: string) {
  sessionStorage.setItem(KEY, JSON.stringify({ text, document_id, title }));
  navigate('id', 'chat');
}
export function takeAssistantTask(): AssistantTask | null {
  const raw = sessionStorage.getItem(KEY);
  if (!raw) return null;
  sessionStorage.removeItem(KEY);
  try { return JSON.parse(raw) as AssistantTask; } catch { return null; }
}

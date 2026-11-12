import { activeProject } from '../../api';
import { navigate } from '../navigation';

export interface AssistantTask { text: string; document_id?: string; title?: string; project?: string }
const KEY = 'bis:assistant-task';
let pending: AssistantTask | null = null;
export function askAssistant(text: string, document_id?: string, title?: string) {
  pending = { text, document_id, title, project: activeProject() };
  try { sessionStorage.setItem(KEY, JSON.stringify(pending)); } catch { /* memory fallback */ }
  navigate('id', 'chat');
}
export function takeAssistantTask(): AssistantTask | null {
  let task = pending; pending = null;
  try {
    const raw = sessionStorage.getItem(KEY); sessionStorage.removeItem(KEY);
    if (!task && raw) task = JSON.parse(raw) as AssistantTask;
  } catch { /* use memory when storage is blocked */ }
  if (!task || typeof task.text !== 'string' || task.project !== activeProject()) return null;
  return task;
}

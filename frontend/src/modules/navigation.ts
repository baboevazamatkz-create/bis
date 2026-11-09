export type Workspace = "id" | "platform";
export interface Route { workspace: Workspace; page: string }
export interface NavGroup { title: string; items: [string, string][] }

export const ID_NAV: NavGroup[] = [
  { title: "Исполнительная документация", items: [["overview", "Рабочий стол"], ["pipeline", "Сообщить о работе"], ["chat", "Помощник ПТО"], ["check", "Комплектность ИД"]] },
  { title: "Документы", items: [["documents", "Реестр ИД"], ["aosr", "Подготовить акт"], ["schemes", "Исполнительные схемы"]] },
  { title: "Исходные данные", items: [["project-reading", "Чтение проекта"], ["facts", "Факты проекта"], ["libraries", "Библиотеки"], ["archive", "Загрузить документы"], ["sources", "РД и документы качества"], ["brain", "Синапс"]] },
  { title: "Объект и участники", items: [["registry:objects", "Объекты"], ["registry:counterparties", "Организации"],
    ["registry:persons", "Представители"], ["registry:appointments", "Назначения"], ["knowledge", "Нормативная база"]] },
  { title: "", items: [["settings", "Настройки"]] },
];
export const PLATFORM_NAV: NavGroup[] = [
  { title: "Платформа", items: [["feed", "Лента ИИ"], ["chat", "Ассистент"], ["brain", "Синапс"], ["control", "Контроль"]] },
  { title: "Направления", items: [["documents", "Документы"], ["contracts", "Договоры"], ["project", "Проект"],
    ["works", "Работы и закрытие"], ["schemes", "Исполнительные схемы"]] },
  { title: "Общие данные", items: [["registry:objects", "Объекты"], ["registry:counterparties", "Контрагенты"],
    ["registry:persons", "Представители"], ["registry:appointments", "Назначения"], ["registry:tasks", "Поручения"],
    ["registry:obligations", "Обязательства"], ["registry:estimate", "Смета"], ["registry:schedule", "График"],
    ["registry:volumes", "Объёмы"], ["registry:materials", "Материалы"], ["registry:norms", "Нормы расхода"],
    ["registry:receipts", "Приход"], ["registry:usage", "Расход"], ["knowledge", "База знаний"]] },
  { title: "Сервис", items: [["specialists", "ИИ-специалисты"], ["archive", "Загрузка архива"], ["settings", "Настройки"]] },
];

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  const workspace = parts[0] === "platform" ? "platform" : "id";
  const page = parts.slice(1).join("/");
  const groups = workspace === "id" ? ID_NAV : PLATFORM_NAV;
  return { workspace, page: groups.some((g) => g.items.some(([id]) => id === page)) ? page : workspace === "id" ? "overview" : "feed" };
}

export function navigate(workspace: Workspace, page: string) {
  window.location.hash = `/${workspace}/${page}`;
}

export type Workspace = "id" | "platform";
export interface Route { workspace: Workspace; page: string }
export interface NavGroup { title: string; items: [string, string][] }

export const ID_NAV: NavGroup[] = [{ title: "Рабочее пространство", items: [
  ["chat", "Помощник"], ["documents", "Исполнительная документация"], ["schemes", "Исполнительные схемы"],
  ["project", "Проект"], ["libraries", "Базы данных"], ["contracts", "Договоры"],
]}];
export const PLATFORM_NAV = ID_NAV;
const ALIASES: Record<string,string> = {
  overview: "chat", feed: "chat", brain: "chat", pipeline: "documents", aosr: "documents", check: "documents",
  "project-reading": "project", facts: "project", sources: "project", archive: "libraries", knowledge: "libraries", settings: "libraries",
};
export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/");
  const requested = parts[0] === "id" || parts[0] === "platform" ? parts.slice(1).join("/") : parts.join("/");
  const page = ALIASES[requested] ?? requested;
  return { workspace: "id", page: ID_NAV[0].items.some(([id]) => id === page) ? page : "chat" };
}
export function navigate(workspace: Workspace, page: string) { window.location.hash = `/${workspace}/${page}`; }

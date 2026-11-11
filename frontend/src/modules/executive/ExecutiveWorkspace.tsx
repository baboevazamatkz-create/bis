import type { User } from "../../api";
import Chat from "../../pages/Chat";
import WorkspaceDocuments from "./WorkspaceDocuments";
import DatabaseHub from "./DatabaseHub";

export default function ExecutiveWorkspace({ page, user }: { page: string; user: User }) {
  if (page === "libraries") return <DatabaseHub user={user} />;
  if (page === "documents" || page === "schemes" || page === "project" || page === "contracts") {
    return <WorkspaceDocuments key={page} category={page} />;
  }
  return <Chat workspace="executive" />;
}

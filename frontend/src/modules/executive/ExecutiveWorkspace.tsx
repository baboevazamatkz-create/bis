import type { User } from "../../api";
import Chat from "../../pages/Chat";
import WorkspaceDocuments from "./WorkspaceDocuments";
import DatabaseHub from "./DatabaseHub";
import ExecutiveSettings from "./ExecutiveSettings";
import ExecutiveDocuments from "./ExecutiveDocuments";

export default function ExecutiveWorkspace({ page, user }: { page: string; user: User }) {
  if (page === "libraries" || page === "libraries/voice") return <DatabaseHub user={user} openVoiceSetup={page === "libraries/voice"} />;
  if (page === "settings") return <ExecutiveSettings user={user} />;
  if (page === "project/new") return <WorkspaceDocuments key={page} category="project" canWrite={user.role !== "viewer"} openProjectForm />;
  if (page === "documents" || page.startsWith("documents/")) {
    return <ExecutiveDocuments page={page} canWrite={user.role !== "viewer"} />;
  }
  if (page === "project" || page === "contracts") {
    return <WorkspaceDocuments key={page} category={page} canWrite={user.role !== "viewer"} />;
  }
  return <Chat workspace="executive" />;
}

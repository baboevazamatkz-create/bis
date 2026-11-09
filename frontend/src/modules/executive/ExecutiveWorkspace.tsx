import type { User } from "../../api";
import Chat from "../../pages/Chat";
import Brain from "../../pages/Brain";
import Documents from "../../pages/Documents";
import AosrForm from "../../pages/AosrForm";
import Schemes from "../../pages/Schemes";
import ArchiveImport from "../../pages/ArchiveImport";
import Knowledge from "../../pages/Knowledge";
import Registry from "../../pages/Registry";
import ExecutiveSettings from "./ExecutiveSettings";
import ExecutiveOverview from "./ExecutiveOverview";
import ProjectReading from "../../pages/ProjectReading";
import ProjectFacts from "../../pages/ProjectFacts";
import Libraries from "../../pages/Libraries";
import ExecutivePipeline from "./ExecutivePipeline";
import { ID_KINDS, SOURCE_KINDS } from "./config";
import { navigate } from "../navigation";

export default function ExecutiveWorkspace({ page, user }: { page: string; user: User }) {
  switch (page) {
    case "project-reading": return <ProjectReading user={user} />;
    case "facts": return <ProjectFacts user={user} />;
    case "libraries": return <Libraries user={user} />;
    case "pipeline": return <ExecutivePipeline user={user} />;
    case "chat": return <Chat workspace="executive" />;
    case "brain": return <Brain executive />;
    case "check": return <ExecutiveOverview checkOnly />;
    case "documents": return <Documents key="records" workspace="executive" allowedKinds={ID_KINDS} title="Реестр исполнительной документации" hideUpload />;
    case "sources": return <Documents key="sources" workspace="executive" collection="sources" allowedKinds={SOURCE_KINDS} title="РД и документы качества" />;
    case "aosr": return <AosrForm onCreated={() => navigate("id", "documents")} />;
    case "schemes": return <Schemes />;
    case "archive": return <ArchiveImport executive />;
    case "knowledge": return <Knowledge user={user} />;
    case "settings": return <ExecutiveSettings />;
    default: return page.startsWith("registry:") ? <Registry key={page} kind={page.split(":")[1]} /> : <ExecutiveOverview />;
  }
}

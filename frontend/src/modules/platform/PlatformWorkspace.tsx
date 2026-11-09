// Other BIS directions live here until their own workspaces are developed.
import { lazy, Suspense } from "react";
import type { User } from "../../api";
const Feed = lazy(() => import("../../pages/Feed"));
const Chat = lazy(() => import("../../pages/Chat"));
const Brain = lazy(() => import("../../pages/Brain"));
const Control = lazy(() => import("../../pages/Control"));
const Contracts = lazy(() => import("../../pages/Contracts"));
const ProjectDocs = lazy(() => import("../../pages/ProjectDocs"));
const Works = lazy(() => import("../../pages/Works"));
const Schemes = lazy(() => import("../../pages/Schemes"));
const Documents = lazy(() => import("../../pages/Documents"));
const Knowledge = lazy(() => import("../../pages/Knowledge"));
const Registry = lazy(() => import("../../pages/Registry"));
const Specialists = lazy(() => import("../../pages/Specialists"));
const ArchiveImport = lazy(() => import("../../pages/ArchiveImport"));
const Settings = lazy(() => import("../../pages/Settings"));

export default function PlatformWorkspace({ page, user }: { page: string; user: User }) {
  let content;
  switch (page) {
    case "chat": content = <Chat />; break;
    case "brain": content = <Brain />; break;
    case "control": content = <Control />; break;
    case "contracts": content = <Contracts />; break;
    case "project": content = <ProjectDocs />; break;
    case "works": content = <Works />; break;
    case "schemes": content = <Schemes />; break;
    case "documents": content = <Documents />; break;
    case "knowledge": content = <Knowledge user={user} />; break;
    case "specialists": content = <Specialists user={user} />; break;
    case "archive": content = <ArchiveImport />; break;
    case "settings": content = <Settings />; break;
    default: content = page.startsWith("registry:") ? <Registry key={page} kind={page.split(":")[1]} /> : <Feed />;
  }
  return <Suspense fallback={<p className="muted">Загрузка раздела…</p>}>{content}</Suspense>;
}

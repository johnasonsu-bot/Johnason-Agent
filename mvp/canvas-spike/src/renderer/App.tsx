import { useEffect, useState } from "react";
import { artifacts } from "./artifacts";
import { ProviderCenter } from "./providers/ProviderCenter";
import { registry } from "./renderers";
import "./styles.css";

const workspaces = [
  ["home", "主页"],
  ["conversations", "会话"],
  ["agents", "Agent"],
  ["tasks", "任务"],
  ["artifacts", "Artifacts"],
] as const;

const settings = [
  ["providers", "模型供应商"],
  ["connectors", "连接器"],
  ["skills", "Skills"],
  ["settings", "设置"],
] as const;

type Workspace = typeof workspaces[number][0];
type Setting = typeof settings[number][0];
type View = Workspace | Setting;

const workspaceIds = new Set<Workspace>(workspaces.map(([id]) => id));
const settingIds = new Set<Setting>(settings.map(([id]) => id));

function readView(): View {
  const hash = window.location.hash.slice(1);
  if (workspaceIds.has(hash as Workspace) || settingIds.has(hash as Setting)) return hash as View;
  return "artifacts";
}

function StatusPanel({ title, children }: { title: string; children: string }) {
  return (
    <section className="status-panel" aria-labelledby={`${title}-title`}>
      <p className="eyebrow">工作区</p>
      <h2 id={`${title}-title`}>{title}</h2>
      <p>{children}</p>
    </section>
  );
}

function WorkspaceContent({ view }: { view: Workspace }) {
  if (view === "artifacts") return <>{artifacts.map((artifact) => {
    const Renderer = registry.resolve(artifact.kind);
    return <Renderer key={artifact.id} artifact={artifact} />;
  })}</>;

  const content: Record<Exclude<Workspace, "artifacts">, string> = {
    home: "主页工作区正在建设中。这里将汇总最近活动、运行状态和快捷入口。",
    conversations: "会话工作区正在建设中。你将能够在这里浏览和继续对话。",
    agents: "Agent 工作区正在建设中。你将能够在这里管理和启动 Agent。",
    tasks: "任务工作区正在建设中。你将能够在这里跟踪执行中的工作。",
  };
  const title = workspaces.find(([id]) => id === view)?.[1] ?? "主页";
  return <StatusPanel title={title}>{content[view]}</StatusPanel>;
}

function SettingsContent({ view }: { view: Setting }) {
  if (view === "providers") return <ProviderCenter />;
  const content: Record<Exclude<Setting, "providers">, string> = {
    connectors: "连接器管理正在建设中。可在此查看和配置外部连接。",
    skills: "Skills 管理正在建设中。可在此查看可用技能及其状态。",
    settings: "应用设置正在建设中。可在此调整工作台偏好。",
  };
  const title = settings.find(([id]) => id === view)?.[1] ?? "设置";
  return <StatusPanel title={title}>{content[view]}</StatusPanel>;
}

export function App() {
  const [view, setView] = useState(readView);
  const [workspace, setWorkspace] = useState<Workspace>(() => {
    const initial = readView();
    return workspaceIds.has(initial as Workspace) ? initial as Workspace : "home";
  });

  useEffect(() => {
    const updateView = () => {
      const nextView = readView();
      setView(nextView);
      if (workspaceIds.has(nextView as Workspace)) setWorkspace(nextView as Workspace);
    };
    window.addEventListener("hashchange", updateView);
    return () => window.removeEventListener("hashchange", updateView);
  }, []);

  const isWorkspace = workspaceIds.has(view as Workspace);
  return (
    <main>
      <header className="app-header">
        <h1>Hermes Workbench</h1>
        <nav className="settings-navigation" aria-label="设置">
          {settings.map(([id, label]) => <a key={id} href={`#${id}`} aria-current={view === id ? "page" : undefined}>{label}</a>)}
        </nav>
      </header>
      <div className="app-layout">
        <aside className="workspace-sidebar">
          <nav className="workspace-navigation" aria-label="工作区">
            {workspaces.map(([id, label]) => <a key={id} href={`#${id}`} aria-current={workspace === id ? "page" : undefined}>{label}</a>)}
          </nav>
        </aside>
        <section className="app-content" aria-live="polite">
          {isWorkspace ? <WorkspaceContent view={view as Workspace} /> : <SettingsContent view={view as Setting} />}
        </section>
      </div>
    </main>
  );
}

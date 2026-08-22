import { useState } from "react";
import { ProviderCenter } from "./providers/ProviderCenter";
import { AgentCenter } from "./agents/AgentCenter";
import { ConversationWorkspace } from "./conversations/ConversationWorkspace";
import { WorkspacePage } from "./workspace/WorkspacePage";
import "./styles.css";

type Page = "home" | "conversations" | "tasks" | "artifacts" | "workspace" | "providers" | "agents" | "connectors" | "skills" | "settings";

const railItems: Array<{ id: Page; icon: string; label: string }> = [
  { id: "home", icon: "⌂", label: "主页" },
  { id: "conversations", icon: "◌", label: "会话" },
  { id: "tasks", icon: "☷", label: "任务" },
  { id: "artifacts", icon: "▤", label: "画布" },
  { id: "workspace", icon: "⌘", label: "Workspace" },
];

function PlaceholderPage({ page }: { page: Page }) {
  const content: Record<string, [string, string]> = {
    home: ["主页 · Home", "查看最近会话、运行中的任务和可用 Workspace。"],
    tasks: ["Agent 任务 · Tasks", "持续任务、监督 Agent 和状态机入口将在这里汇总。"],
    artifacts: ["智能画布 · Artifacts", "来自会话的报告、运行图谱、表格、HTML 和音频产物。"],
    connectors: ["连接器 · Connectors", "本地文件、Data Platform 和其他软件模块的连接状态。"],
    skills: ["Skills", "管理可被 @ 调用的技能目录。"],
    settings: ["设置 · Settings", "工作台偏好、日志和实验开关。"],
  };
  const [title, description] = content[page] ?? content.home;
  return <section className="placeholder-page"><p className="eyebrow">Agent Workbench · V4</p><h1>{title}</h1><p>{description}</p><div className="placeholder-grid"><div><strong>持续上下文</strong><span>会话级上下文与项目共享上下文分离保存。</span></div><div><strong>可观测执行</strong><span>AG-UI timeline 显示 Agent、工具、决策和状态。</span></div><div><strong>可扩展画布</strong><span>文本之外支持图形、表格、HTML 和音频产物。</span></div></div></section>;
}

export function App() {
  const [page, setPage] = useState<Page>("conversations");
  const openPage = (next: Page) => setPage(next);
  const renderMain = () => {
    if (page === "workspace") return <WorkspacePage onBack={() => setPage("conversations")} />;
    if (page === "providers") return <section className="provider-page"><ProviderCenter /></section>;
    if (page === "agents") return <section className="provider-page"><AgentCenter /></section>;
    if (page === "conversations" || page === "artifacts") return <ConversationWorkspace />;
    return <PlaceholderPage page={page} />;
  };
  return <main className="v4-shell">
    <header className="v4-topbar"><div className="v4-brand"><span className="v4-brand-mark">AW</span><div><strong>Generic Agent</strong><small>本地 Agent Workbench · Batch2</small></div></div><div className="v4-topbar-context"><span className="v4-connection-dot">●</span><span>Task2 Runtime · Task3 Conversation API</span></div><nav className="v4-settings-nav" aria-label="设置"><a href="#providers" className={page === "providers" ? "active" : ""} aria-current={page === "providers" ? "page" : undefined} onClick={(event) => { event.preventDefault(); openPage("providers"); }}>模型供应商</a><button type="button" className={page === "agents" ? "active" : ""} aria-current={page === "agents" ? "page" : undefined} onClick={() => openPage("agents")}>Agent 配置</button><button type="button" onClick={() => openPage("connectors")}>连接器</button><button type="button" onClick={() => openPage("skills")}>Skills</button><button type="button" onClick={() => openPage("settings")}>设置</button></nav></header>
    <div className="v4-body"><aside className="v4-rail"><div className="v4-rail-title">WORKBENCH</div><nav className="v4-rail-nav" aria-label="主导航">{railItems.map((item) => <button key={item.id} type="button" className={page === item.id ? "active" : ""} aria-current={page === item.id ? "page" : undefined} onClick={() => openPage(item.id)}><span>{item.icon}</span><small>{item.label}</small></button>)}</nav><div className="v4-rail-bottom"><span className="v4-user-avatar">苏</span><small>本地</small></div></aside><section className="v4-content">{renderMain()}</section></div>
  </main>;
}

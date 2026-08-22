import { useState } from "react";

type WorkspaceKind = "cloud" | "local";

const workspaces = [
  { id: "cloud", kind: "cloud" as const, title: "项目云端 · Data Platform", description: "共享任务、运行实例和产物。适合多人 Agent 协同与长任务恢复。", status: "已连接", icon: "☁", fields: ["项目：Generic-Agent / job-73", "最近运行：instance-86 · completed"] },
  { id: "local", kind: "local" as const, title: "本地项目 · generic-agent", description: "直接读取本地文件、运行工具和预览产物，不依赖云端项目。", status: "可用", icon: "⌂", fields: ["路径：/Users/sushi/Downloads/generic-agent", "连接器：本地文件 · Shell · Browser"] },
];

export function WorkspacePage({ onBack }: { onBack: () => void }) {
  const [filter, setFilter] = useState<"all" | WorkspaceKind>("all");
  const [selected, setSelected] = useState<WorkspaceKind>("cloud");
  const visible = workspaces.filter((workspace) => filter === "all" || workspace.kind === filter);
  return <section className="workspace-page" aria-label="Workspace 工作空间"><header className="workspace-page-head"><div><p className="eyebrow">Workspace context</p><h2>Workspace · 工作空间</h2><p>选择本轮会话使用的云端或本地项目上下文</p></div><button type="button" className="quiet workspace-back" onClick={onBack}>← 返回会话</button></header><div className="workspace-page-body"><div className="workspace-filter"><button type="button" className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部</button><button type="button" className={filter === "cloud" ? "active" : ""} onClick={() => setFilter("cloud")}>云端</button><button type="button" className={filter === "local" ? "active" : ""} onClick={() => setFilter("local")}>本地</button></div><div className="workspace-cards">{visible.map((workspace) => <article className={`workspace-card ${selected === workspace.kind ? "selected" : ""}`} key={workspace.id}><header><div><h3>{workspace.title}</h3><span className="workspace-status"><i />{workspace.status}</span></div><span className="workspace-kind">{workspace.icon}</span></header><p>{workspace.description}</p>{workspace.fields.map((field) => <div className="workspace-field" key={field}>{field}</div>)}<div className="workspace-actions"><button type="button" onClick={() => setSelected(workspace.kind)}>{selected === workspace.kind ? "当前使用中" : "使用此空间"}</button><button type="button" className="quiet">查看详情</button></div></article>)}</div></div></section>;
}

import { useState } from "react";
import { artifacts } from "../artifacts";
import { registry } from "../renderers";
import { Composer } from "./Composer";
import { SessionSidebar } from "./SessionSidebar";
import { Timeline, type TimelineEntry } from "./Timeline";

const initialTimeline: TimelineEntry[] = [
  { id: "started", kind: "agent", title: "产品经理 · Product Manager", content: "已接收任务，正在确定项目文件范围。", agent: "产品经理 / LM Studio" },
  { id: "plan", kind: "decision", title: "决策 · Inspection plan", content: "先读取根目录说明和工作区配置，再生成可验证的文件清单。" },
  { id: "step", kind: "step", title: "步骤 1/2 · Scan workspace", content: "扫描项目根目录与受版本控制文件。" },
];

export function ConversationWorkspace() {
  const [entries, setEntries] = useState<TimelineEntry[]>(initialTimeline);
  const [group, setGroup] = useState<string[]>([]);
  const [artifactId, setArtifactId] = useState("markdown");
  const [canvasOpen, setCanvasOpen] = useState(true);
  const artifact = artifacts.find((candidate) => candidate.id === artifactId) ?? artifacts[0];
  const Renderer = registry.resolve(artifact.kind);
  const send = (prompt: string) => {
    setEntries((current) => [...current,
      { id: `user-${Date.now()}`, kind: "user", title: "人工介入 · Human", content: prompt },
      { id: `delta-${Date.now()}`, kind: "delta", title: "Assistant response", content: "已开始处理：正在列出项目文件并整理用途。", agent: "工程师 / LM Studio" },
      { id: `tool-${Date.now()}`, kind: "tool", title: "工具执行完成", content: "workspace.list → 已检索到 4 个项目文件", agent: "工具证据 / local fixture" },
      { id: `answer-${Date.now()}`, kind: "agent", title: "执行结果", content: "README.md\npackage.json\nmvp/\ndocs/\n\n已将文件清单同步到智能画布。", agent: "工程师 / local-agent" },
    ]);
    setArtifactId("markdown");
    setCanvasOpen(true);
  };
  return <section aria-label="会话工作区" style={{ display: "flex", minHeight: "calc(100vh - 105px)", borderTop: "1px solid #e6e5e2", background: "#fff" }}>
    <SessionSidebar group={group} onCreateGroup={(roles) => setGroup(roles)} />
    <div style={{ flex: 1, minWidth: 0, display: "grid", gridTemplateRows: "1fr auto" }}><Timeline entries={entries} group={group.map((role) => `${role} · ${role === "产品经理" ? "Product Manager" : role === "架构师" ? "Architect" : role === "工程师" ? "Engineer" : role}`)} /><Composer onSend={send} /></div>
    {canvasOpen ? <aside aria-label="Artifacts Canvas" style={{ width: "clamp(260px, 25vw, 360px)", borderLeft: "1px solid #e6e5e2", background: "#fff", overflow: "auto" }}><header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "15px 14px", borderBottom: "1px solid #eef0f2" }}><strong>智能画布 · Artifacts</strong><button type="button" className="quiet" onClick={() => setCanvasOpen(false)}>折叠</button></header><nav aria-label="Artifacts 列表" style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: 12 }}>{artifacts.map((item) => <button key={item.id} type="button" className="quiet" aria-pressed={artifactId === item.id} onClick={() => setArtifactId(item.id)} style={{ fontSize: 12 }}>{item.title}</button>)}</nav><section style={{ margin: 12, border: "1px solid #e5e7eb", borderRadius: 9, padding: 12 }}><small style={{ color: "#6b7280" }}>version v3 · {artifact.mimeType}</small><h3 style={{ marginTop: 8 }}>{artifact.title}</h3><Renderer artifact={artifact} /></section><section style={{ margin: 12, border: "1px solid #e5e7eb", borderRadius: 9, padding: 12 }}><strong>版本卡片 · Version cards</strong><p style={{ color: "#6b7280", fontSize: 13, margin: "8px 0 0" }}>v3 当前结果 · v2 人工批注 · v1 原始证据</p></section></aside> : <button type="button" className="quiet" onClick={() => setCanvasOpen(true)} style={{ writingMode: "vertical-rl", borderRadius: 0 }}>打开画布</button>}
    {group.length > 0 && <div data-testid="agent-avatar-stack" aria-label="Agent avatar stack" style={{ position: "absolute", top: 99, right: canvasOpen ? 385 : 50, display: "flex", alignItems: "center", zIndex: 3 }}><span style={{ background: "#0e2841", color: "#fff", borderRadius: "50%", padding: "5px 8px", border: "2px solid #fff" }}>{group[0]?.slice(0, 1)}</span><span style={{ background: "#00a9f4", color: "#fff", borderRadius: "50%", padding: "5px 8px", marginLeft: -7, border: "2px solid #fff" }}>{group[1]?.slice(0, 1)}</span><span style={{ background: "#5f6368", color: "#fff", borderRadius: 999, padding: "5px 8px", marginLeft: -7, border: "2px solid #fff" }}>+{Math.max(0, group.length - 2)}</span></div>}
  </section>;
}

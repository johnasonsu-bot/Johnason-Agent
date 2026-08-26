import { useState } from "react";
import { AgentPicker, type AgentPickerResult, agents } from "./AgentPicker";
import type { AgentModelProfile } from "../models/agentConfig";

type Session = { id: string; title: string; status: string; group: string[] };
const SESSION_STORAGE_KEY = "hermes.v4.session-index";

const initialSessions: Session[] = [
  { id: "ui-session-0", title: "Jira 看板配置修复指引", status: "14:38 · 运行完成", group: ["产品经理", "架构师", "工程师"] },
  { id: "ui-session-1", title: "方案评审 · Architecture review", status: "昨日 · 等待人工补充", group: ["架构师"] },
  { id: "ui-session-2", title: "本地文件整理", status: "昨日 · archived", group: ["工程师"] },
  { id: "ui-session-3", title: "数据平台任务调试", status: "周二 · 157 条结果", group: ["产品经理", "工程师"] },
];

export function SessionSidebar({ onCreateGroup, onSessionChange, activeSessionId, group, agentProfiles }: { onCreateGroup: (roles: string[], mode: "single" | "multi") => { sessionId: string; title: string }; onSessionChange: (sessionId: string) => void; activeSessionId: string; group: string[]; agentProfiles?: AgentModelProfile[] }) {
  const [sessions, setSessions] = useState<Session[]>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) ?? "[]") as Session[];
      return [...initialSessions, ...saved.filter((item) => item && !initialSessions.some((seed) => seed.id === item.id))];
    } catch {
      return initialSessions;
    }
  });
  const [pickerOpen, setPickerOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [role, setRole] = useState("产品经理");
  const [roleOpen, setRoleOpen] = useState(false);
  const create = (result: AgentPickerResult) => {
    const names = result.selected.map((agent) => agent.name);
    const title = result.mode === "single" ? `${names[0]} · 新任务` : `${names.slice(0, 3).join("、")}${names.length > 3 ? " 等" : ""} · 协同会话`;
    const created = onCreateGroup(names, result.mode);
    const session = { id: created.sessionId, title, status: "刚刚创建 · ready", group: names };
    setSessions((current) => [session, ...current.filter((item) => item.id !== session.id)]);
    try {
      const saved = JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY) ?? "[]") as Session[];
      localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify([session, ...saved.filter((item) => item.id !== session.id)]));
    } catch {
      // The backend remains the source of truth for events if browser storage is unavailable.
    }
    onSessionChange(created.sessionId);
  };
  return <aside className="session-sidebar" aria-label="会话与任务">
    <div className="session-sidebar-head"><p className="eyebrow">Agent Workbench · V4</p><h2>会话 · Conversations</h2><button type="button" className="new-session-button" onClick={() => setPickerOpen(true)}>＋ 新建会话</button><div className="session-sidebar-actions"><button type="button" className="quiet" onClick={() => setPickerOpen(true)}>选择 Agent</button><button type="button" className="quiet" aria-label="任务历史" onClick={() => setHistoryOpen((open) => !open)}>历史</button></div></div>
    <div className="session-filter"><button type="button" className="filter-pill active">全部</button><button type="button" className="filter-pill">进行中</button><button type="button" className="filter-pill">已归档</button></div>
    <nav className="session-list" aria-label="会话列表">{sessions.map((session) => <button key={session.id} type="button" className={`session-list-item ${session.id === activeSessionId ? "active" : ""}`} onClick={() => onSessionChange(session.id)}><strong>{session.title}</strong><small>{session.status}</small></button>)}</nav>
    <div className="role-switcher"><button type="button" className="role-current" aria-label={`当前角色 ${role}`} onClick={() => setRoleOpen((open) => !open)}>◉ {role}<small>Current Claw · 本地工作台</small></button>{roleOpen && <div className="role-menu" role="menu" aria-label="角色与设置菜单">{(agentProfiles?.length ? agentProfiles : agents).map((agent) => <button key={agent.id} type="button" className="quiet" role="menuitem" onClick={() => { setRole(agent.name); setRoleOpen(false); }}>{agent.name}</button>)}<hr />{["角色市场", "配置当前 Claw", "换头像", "深色外观", "打开工作空间", "上传/导出日志"].map((item) => <button key={item} type="button" className="quiet" role="menuitem" disabled>{item}</button>)}</div>}</div>
    {historyOpen && <div className="history-popover" role="dialog" aria-label="任务历史"><div className="history-head"><strong>任务历史</strong><button type="button" className="quiet" onClick={() => setHistoryOpen(false)}>关闭</button></div><p>Jira 看板配置修复指引</p><small>14:38 · 已完成 8m 22s</small><p>多 Agent 协同会话</p><small>{group.length ? `${group.length} Agents · active` : "尚未创建"}</small></div>}
    <AgentPicker open={pickerOpen} onClose={() => setPickerOpen(false)} onCreate={create} profiles={agentProfiles} />
  </aside>;
}

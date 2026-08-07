import { useState } from "react";

const roles = ["产品经理", "架构师", "工程师", "测试工程师", "敏捷教练", "DevOps", "数智员工导师"];
const sessions = ["Jira 看板配置修复指引", "方案评审 · Architecture review", "本地文件整理"];

export function SessionSidebar({ onCreateGroup, onSessionChange, group }: { onCreateGroup: (roles: string[]) => void; onSessionChange: (sessionId: string) => void; group: string[] }) {
  const [role, setRole] = useState("产品经理");
  const [roleMenu, setRoleMenu] = useState(false);
  const [agentPicker, setAgentPicker] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [history, setHistory] = useState(false);
  const toggle = (value: string) => setSelected((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);

  return <aside aria-label="会话与任务" style={{ display: "flex", flexDirection: "column", minWidth: 218, borderRight: "1px solid #e6e5e2", background: "#fff", position: "relative" }}>
    <div style={{ padding: "18px 14px 12px", borderBottom: "1px solid #eef0f2" }}>
      <strong>会话 · Conversations</strong>
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}><button className="quiet" type="button" onClick={() => setAgentPicker(true)}>选择 Agent</button><button className="quiet" type="button" onClick={() => setHistory(true)} aria-label="任务历史">历史</button></div>
    </div>
    <nav aria-label="会话列表" style={{ display: "grid", gap: 3, padding: 10 }}>
      {sessions.map((session, index) => <button key={session} type="button" className="quiet" onClick={() => onSessionChange(`ui-session-${index}`)} style={{ textAlign: "left", border: 0, padding: "10px 8px", background: index === 0 ? "#eaf7fd" : "transparent" }}>{session}<small style={{ display: "block", color: "#6b7280", marginTop: 4 }}>{index === 0 ? "14:38 · 运行完成" : "昨日 · archived"}</small></button>)}
    </nav>
    <div style={{ marginTop: "auto", borderTop: "1px solid #eef0f2", padding: 12 }}>
      <button type="button" className="quiet" aria-label={`当前角色 ${role}`} onClick={() => setRoleMenu((open) => !open)} style={{ width: "100%", textAlign: "left" }}>◉ {role}<small style={{ display: "block", marginLeft: 19, color: "#6b7280" }}>Current Claw</small></button>
      {roleMenu && <div role="menu" aria-label="角色与设置菜单" style={{ position: "absolute", bottom: 72, left: 12, width: 244, padding: 8, background: "#fff", border: "1px solid #d9dce1", borderRadius: 10, boxShadow: "0 10px 28px rgba(0,0,0,.13)", zIndex: 4 }}>
        {roles.map((candidate) => <button key={candidate} type="button" role="menuitem" className="quiet" onClick={() => { setRole(candidate); setRoleMenu(false); }} style={{ width: "100%", border: 0, textAlign: "left" }}>{candidate}</button>)}
        <hr style={{ border: 0, borderTop: "1px solid #eef0f2" }} />
        {["角色市场", "配置当前 Claw", "换头像", "深色外观", "打开工作空间", "上传/导出日志"].map((item) => <button key={item} type="button" role="menuitem" className="quiet" disabled title="该入口将在后续批次接入" style={{ width: "100%", border: 0, textAlign: "left" }}>{item} · 即将推出</button>)}
      </div>}
    </div>
    {agentPicker && <div role="dialog" aria-label="选择多个 Agent" style={{ position: "fixed", inset: 0, display: "grid", placeItems: "center", background: "rgba(5,28,44,.24)", zIndex: 8 }}><section style={{ width: "min(500px, 92vw)", background: "#fff", borderRadius: 12, padding: 24, boxShadow: "0 18px 60px rgba(0,0,0,.2)" }}><h2 style={{ fontSize: 21 }}>选择 Agent · Create group chat</h2><p style={{ color: "#6b7280" }}>选择至少三名角色创建多 Agent 会话。</p><div style={{ display: "grid", gap: 8 }}>{roles.map((candidate) => <label key={candidate} style={{ display: "flex", alignItems: "center", gap: 9, fontWeight: 500 }}><input type="checkbox" aria-label={candidate} checked={selected.includes(candidate)} onChange={() => toggle(candidate)} />{candidate}</label>)}</div><div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 20 }}><button type="button" className="quiet" onClick={() => setAgentPicker(false)}>取消</button><button type="button" disabled={selected.length < 3} onClick={() => { onCreateGroup(selected); setAgentPicker(false); }}>创建群聊</button></div></section></div>}
    {history && <div role="dialog" aria-label="单任务历史" style={{ position: "fixed", right: 22, top: 82, width: 340, background: "#fff", border: "1px solid #d9dce1", borderRadius: 10, padding: 18, boxShadow: "0 10px 28px rgba(0,0,0,.13)", zIndex: 7 }}><div style={{ display: "flex", justifyContent: "space-between" }}><strong>任务历史</strong><button type="button" className="quiet" onClick={() => setHistory(false)}>关闭</button></div><p style={{ margin: "16px 0 5px" }}>Jira 看板配置修复指引</p><small style={{ color: "#6b7280" }}>14:38 · 已完成 8m 22s</small><p style={{ margin: "16px 0 5px" }}>群聊会话管理</p><small style={{ color: "#6b7280" }}>{group.length ? `${group.length} Agents` : "尚未创建"}</small></div>}
  </aside>;
}

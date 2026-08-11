import { useMemo, useState } from "react";

export type AgentProfile = {
  id: string;
  name: string;
  role: string;
  provider: string;
  color: string;
  glyph: string;
};

export const agents: AgentProfile[] = [
  { id: "pm", name: "产品经理", role: "Product Manager", provider: "LM Studio", color: "blue", glyph: "产" },
  { id: "architect", name: "架构师", role: "Architecture Agent", provider: "DeepSeek V4 Flash", color: "purple", glyph: "架" },
  { id: "engineer", name: "工程师", role: "Engineering Agent", provider: "LM Studio", color: "green", glyph: "工" },
  { id: "qa", name: "测试工程师", role: "QA Agent", provider: "DeepSeek V4 Flash", color: "orange", glyph: "测" },
  { id: "coach", name: "敏捷教练", role: "Agile Coach", provider: "LM Studio", color: "teal", glyph: "敏" },
  { id: "devops", name: "DevOps", role: "DevOps Agent", provider: "LM Studio", color: "dark", glyph: "D" },
];

export type AgentPickerResult = {
  mode: "single" | "multi";
  selected: AgentProfile[];
};

export function AgentPicker({ open, onClose, onCreate }: { open: boolean; onClose: () => void; onCreate: (result: AgentPickerResult) => void }) {
  const [mode, setMode] = useState<"single" | "multi">("single");
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selected = selectedIds.map((id) => agents.find((agent) => agent.id === id)).filter((agent): agent is AgentProfile => Boolean(agent));
  const visible = useMemo(() => agents.filter((agent) => `${agent.name} ${agent.role} ${agent.provider}`.toLowerCase().includes(query.toLowerCase())), [query]);

  if (!open) return null;
  const toggle = (id: string) => {
    setSelectedIds((current) => {
      if (mode === "single") return current[0] === id ? [] : [id];
      return current.includes(id) ? current.filter((item) => item !== id) : [...current, id];
    });
  };
  const canCreate = mode === "single" ? selected.length === 1 : selected.length >= 2;
  const changeMode = (value: "single" | "multi") => {
    setMode(value);
    setSelectedIds(value === "single" ? selectedIds.slice(0, 1) : selectedIds);
  };
  const resetAndClose = () => {
    setQuery("");
    setSelectedIds([]);
    setMode("single");
    onClose();
  };

  return <div className="agent-picker-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) resetAndClose(); }}>
    <section className="agent-picker" role="dialog" aria-modal="true" aria-label="新建会话">
      <header className="agent-picker-head"><div><p className="eyebrow">Conversation setup</p><h2>新建会话 · New conversation</h2></div><button type="button" className="quiet" aria-label="关闭新建会话" onClick={resetAndClose}>×</button></header>
      <div className="agent-picker-body">
        <section className="agent-picker-list">
          <input className="agent-search" aria-label="搜索 Agent" placeholder="搜索 Agent / Claw" value={query} onChange={(event) => setQuery(event.target.value)} />
          <div className="agent-mode-tabs"><button type="button" className={mode === "single" ? "active" : ""} onClick={() => changeMode("single")}>单 Agent</button><button type="button" className={mode === "multi" ? "active" : ""} onClick={() => changeMode("multi")}>多 Agent</button></div>
          <p className="agent-picker-help">{mode === "single" ? "选择 1 个 Agent 开始独立会话；后续可通过 @ 添加协作者。" : "选择至少 2 个 Agent，创建共享上下文的协作会话。"}</p>
          <div className="agent-options">
            {visible.map((agent) => <div key={agent.id} className={`agent-option ${selectedIds.includes(agent.id) ? "selected" : ""}`}>
              <input type="checkbox" aria-label={`选择 ${agent.name}`} checked={selectedIds.includes(agent.id)} onChange={() => toggle(agent.id)} />
              <span className={`agent-avatar agent-avatar-${agent.color}`}>{agent.glyph}</span>
              <button type="button" className="agent-option-button" aria-label={agent.name} onClick={() => toggle(agent.id)}><strong>{agent.name}</strong><small>{agent.role} · {agent.provider}</small></button>
              <span aria-hidden="true" className="agent-check">✓</span>
            </div>)}
          </div>
        </section>
        <aside className="agent-selection-summary"><h3>已选择 <span>{selected.length}</span> 个</h3><p>{canCreate ? "可以创建会话。" : mode === "single" ? "先选择一个 Agent。" : "至少选择两个 Agent。"}</p><div className="selected-agent-list">{selected.length ? selected.map((agent) => <div className="selected-agent" key={agent.id}><span className={`agent-avatar agent-avatar-${agent.color}`}>{agent.glyph}</span><span><strong>{agent.name}</strong><small>{agent.role}</small></span><button type="button" className="quiet" aria-label={`移除 ${agent.name}`} onClick={() => toggle(agent.id)}>×</button></div>) : <div className="selected-empty">点击左侧 Agent 加入会话</div>}</div><p className="agent-selection-note">多人会话会为每个 Agent 保留独立上下文，并由 supervisor 汇总状态。</p></aside>
      </div>
      <footer className="agent-picker-foot"><button type="button" className="quiet" onClick={resetAndClose}>取消</button><button type="button" disabled={!canCreate} onClick={() => { onCreate({ mode, selected }); resetAndClose(); }}>创建会话</button></footer>
    </section>
  </div>;
}

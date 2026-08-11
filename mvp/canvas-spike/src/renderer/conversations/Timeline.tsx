export type TimelineEntry = {
  id: string;
  kind: "user" | "delta" | "decision" | "tool" | "step" | "agent";
  title: string;
  content: string;
  agent?: string;
  status?: string;
};

const kindLabel: Record<TimelineEntry["kind"], string> = {
  user: "人工介入",
  delta: "流式输出",
  decision: "决策",
  tool: "工具证据",
  step: "执行步骤",
  agent: "Agent",
};

export function Timeline({ entries, provider = "LM Studio", model = "local-agent", group, status }: { entries: TimelineEntry[]; provider?: string; model?: string; group: string[]; status: string }) {
  return <section className="timeline" aria-label="AG-UI conversation timeline">
    <header className="timeline-header"><div><p className="eyebrow">AG-UI Timeline</p><h2>项目实施讨论 · Project execution</h2></div><div className="timeline-badges"><span data-testid="model-badge" className="model-pill">{provider} · {model}</span><span className={`status-pill ${status.includes("执行") ? "running" : ""}`}>● {status}</span></div></header>
    <div className="timeline-track"><div className="timeline-day">今天 · 持续上下文已恢复</div>{entries.map((entry) => <article key={entry.id} className="timeline-event"><span className={`timeline-node ${entry.kind}`} aria-hidden="true" /><div className={`timeline-card ${entry.kind === "user" ? "user" : ""}`}><div className="timeline-meta"><strong>{entry.title}</strong><span>{entry.status ?? kindLabel[entry.kind]}</span></div>{entry.agent && <p className="timeline-agent">{entry.agent}</p>}<p className="timeline-content">{entry.content}</p></div></article>)}{group.slice(1).map((agent) => <article key={`agent-${agent}`} className="timeline-event"><span className="timeline-node agent" aria-hidden="true" /><div className="timeline-card"><div className="timeline-meta"><strong>{agent} · Agent</strong><span>协作成员</span></div><p className="timeline-content">已加入协作会话，等待分配下一步。</p></div></article>)}</div>
  </section>;
}

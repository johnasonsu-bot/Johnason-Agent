export type TimelineEntry = {
  id: string;
  kind: "user" | "delta" | "decision" | "tool" | "step" | "agent";
  title: string;
  content: string;
  agent?: string;
};

const kindLabel: Record<TimelineEntry["kind"], string> = {
  user: "人工介入",
  delta: "流式输出",
  decision: "决策",
  tool: "工具证据",
  step: "执行步骤",
  agent: "Agent",
};

export function Timeline({ entries, provider = "LM Studio", model = "local-agent", group }: { entries: TimelineEntry[]; provider?: string; model?: string; group: string[] }) {
  return <section aria-label="AG-UI conversation timeline" style={{ minWidth: 0, overflow: "auto", padding: "20px 22px", background: "#fbfbfa" }}>
    <header style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 20 }}>
      <div>
        <p className="eyebrow">AG-UI Timeline</p>
        <h2 style={{ fontSize: 20, marginBottom: 0 }}>项目实施讨论 · Project execution</h2>
      </div>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
        <span data-testid="model-badge" style={{ border: "1px solid #b7d6e8", color: "#07557b", background: "#eef9ff", borderRadius: 999, padding: "5px 9px", fontSize: 12 }}>{provider} · {model}</span>
        <span style={{ borderRadius: 999, padding: "5px 9px", background: "#eaf7fd", color: "#07557b", fontSize: 12 }}>● 已完成 · completed</span>
      </div>
    </header>
    <div style={{ borderLeft: "2px solid #dce5e9", marginLeft: 8, paddingLeft: 18, display: "grid", gap: 13 }}>
      {entries.map((entry) => <article key={entry.id} style={{ position: "relative", border: "1px solid #e5e7eb", borderRadius: 10, background: "#fff", padding: "12px 14px" }}>
        <span aria-hidden="true" style={{ position: "absolute", left: -25, top: 17, width: 11, height: 11, borderRadius: "50%", background: entry.kind === "tool" ? "#00a9f4" : "#0e2841" }} />
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}><strong>{entry.title}</strong><small style={{ color: "#6b7280" }}>{kindLabel[entry.kind]}</small></div>
        {entry.agent && <p style={{ margin: "7px 0 0", color: "#07557b", fontSize: 13 }}>{entry.agent}</p>}
        <p style={{ margin: "7px 0 0", color: "#374151", whiteSpace: "pre-wrap", lineHeight: 1.55 }}>{entry.content}</p>
      </article>)}
      {group.slice(1).map((agent) => <article key={agent} style={{ position: "relative", border: "1px solid #e5e7eb", borderRadius: 10, background: "#fff", padding: "12px 14px" }}>
        <span aria-hidden="true" style={{ position: "absolute", left: -25, top: 17, width: 11, height: 11, borderRadius: "50%", background: "#0e2841" }} />
        <strong>{agent}</strong><p style={{ margin: "7px 0 0", color: "#374151" }}>已加入协作会话，等待分配下一步。</p>
      </article>)}
    </div>
  </section>;
}

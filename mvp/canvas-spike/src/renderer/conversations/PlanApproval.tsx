import { useState } from "react";
import { graphPlanApi, type ResearchPlan } from "../api";
import { createConversationCommandId } from "./commandIds";

export function PlanApproval({ sessionId, onApproved }: { sessionId: string; onApproved(plan: ResearchPlan): void }) {
  const [plan, setPlan] = useState<ResearchPlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generate = async () => {
    setBusy(true); setError("");
    try { setPlan(await graphPlanApi.propose(sessionId, "针对公开资料形成带证据的竞争分析报告", createConversationCommandId("research-plan", sessionId))); }
    catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };
  const approve = async () => {
    if (!plan) return;
    setBusy(true); setError("");
    try { const approved = await graphPlanApi.approve(sessionId, plan.plan_id, plan.version, createConversationCommandId("research-approve", sessionId)); setPlan(approved); onApproved(approved); }
    catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };
  if (!plan) return <section className="research-plan-launch"><button type="button" onClick={generate} disabled={busy}>生成研究计划</button>{error && <p role="alert">{error}</p>}</section>;
  return <section className="research-plan" aria-label="执行计划">
    <header><div><small>Research Blueprint · v{plan.version}</small><h3>{plan.goal}</h3></div><span>{plan.status === "approved" ? "已批准" : "等待批准"}</span></header>
    <p><strong>{plan.parallel_worker_count} 个并行 Worker</strong> · 并发上限 {plan.max_concurrency} · {plan.temporary_agents.length} 个临时 Agent 提案</p>
    <div className="research-plan-nodes">{plan.nodes.map((node) => <article key={node.node_id}><strong>{node.semantic_role}</strong><small>{node.display_name} · {node.provider_id}/{node.model}</small><em>{node.agent_origin === "temporary_proposal" ? "待批准临时 Agent" : "已配置 Agent"}</em></article>)}</div>
    <footer><span>输出：{plan.artifact_contract.media_type} · 证据映射/限制/未决问题</span>{plan.status === "draft" && <button type="button" onClick={approve} disabled={busy}>批准并执行</button>}</footer>
    {error && <p role="alert">{error}</p>}
  </section>;
}

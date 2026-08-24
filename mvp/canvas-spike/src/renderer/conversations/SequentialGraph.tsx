import type { AgentModelProfile } from "../models/agentConfig";
import { ReviewCard } from "./ReviewCard";
import type { SequentialAttempt, SequentialViewState } from "./sequentialReducer";

export function SequentialGraph({ state, profiles, onApprove }: { state: SequentialViewState; profiles: AgentModelProfile[]; onApprove: () => void }) {
  const progress = Object.values(state.attempts).sort((left, right) => left.sequence - right.sequence || left.attempt - right.attempt);
  const grouped = new Map<string, SequentialAttempt[]>();
  for (const item of progress) {
    const key = `${item.nodeId}:${item.attempt}`;
    grouped.set(key, [...(grouped.get(key) ?? []), item]);
  }
  const attempts = [...grouped.values()].map((records) => records.at(-1)!).sort((left, right) => left.sequence - right.sequence);
  if (!state.graphRunId && progress.length === 0) return null;
  return <section className="sequential-graph" aria-label="多 Agent 执行图">
    <header><div><p className="eyebrow">Persistent LangGraph</p><h3>顺序执行与审核回路</h3></div><small>{state.graphRunId}</small></header>
    <div className="sequential-node-list">{attempts.map((attempt, index) => {
      const profile = profiles.find((candidate) => candidate.id === attempt.agentId);
      const stages = grouped.get(`${attempt.nodeId}:${attempt.attempt}`) ?? [attempt];
      return <article className="sequential-node-card" key={`${attempt.nodeId}:${attempt.attempt}`}>
        <span className="sequential-order">{index + 1}</span><div><strong>{profile?.name ?? attempt.agentId} · Attempt {attempt.attempt}</strong><p>{profile ? `${profile.providerId} / ${profile.model}` : attempt.nodeId}</p><small>{stages.map((stage) => stage.stage).join(" → ")} · {attempt.status}{attempt.percentage !== undefined ? ` · ${attempt.percentage}%` : ""}</small></div>
      </article>;
    })}</div>
    <div className="review-list">{Object.values(state.reviews).map((review) => <ReviewCard key={`${review.reviewerNodeId}:${review.reviewedAttempt}`} review={review} attempts={attempts} profiles={profiles} />)}</div>
    {Object.values(state.warnings).map((warning) => <p className="sequential-warning" role="status" key={`${warning.nodeId}:${warning.attempt}:${warning.code}`}>⚠ 未检测到有效进展：{warning.nodeId} · Attempt {warning.attempt}</p>)}
    {state.interrupted && <div className="human-approval" role="alert"><span>审核需要人工确认后继续执行。</span><button type="button" onClick={onApprove}>批准并继续</button></div>}
  </section>;
}

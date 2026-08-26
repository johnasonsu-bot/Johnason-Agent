import type { AgentModelProfile } from "../models/agentConfig";
import type { SequentialAttempt, SequentialReview } from "./sequentialReducer";

export function ReviewCard({ review, attempts, profiles }: { review: SequentialReview; attempts: SequentialAttempt[]; profiles: AgentModelProfile[] }) {
  const reviewer = attempts.find((attempt) => attempt.nodeId === review.reviewerNodeId)?.agentId;
  const name = profiles.find((profile) => profile.id === reviewer)?.name ?? reviewer ?? "Reviewer";
  const label = review.decision === "approved" ? "审核通过" : review.decision === "rejected" ? "已打回返工" : "等待人工审批";
  return <article className={`review-card ${review.decision}`} aria-label={`${name} 审核`}>
    <header><strong>{name} · 第 {review.reviewedAttempt} 轮{label}</strong><span>{review.decision}</span></header>
    {review.findings.length > 0 && <ul>{review.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul>}
    {review.reworkInstructions && <p><b>返工要求：</b>{review.reworkInstructions}</p>}
    <small>证据：{review.evidenceRefs.join(" · ")}</small>
  </article>;
}

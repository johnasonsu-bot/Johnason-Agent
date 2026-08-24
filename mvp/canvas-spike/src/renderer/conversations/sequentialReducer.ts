import type { ConversationEvent } from "../api";

export type SequentialAttempt = { nodeId: string; agentId: string; attempt: number; stage: string; status: string; sequence: number; percentage?: number };
export type SequentialReview = { reviewerNodeId: string; reviewedNodeId: string; reviewedAttempt: number; decision: "approved" | "rejected" | "needs_human"; findings: string[]; evidenceRefs: string[]; reworkInstructions?: string };
export type SequentialArtifact = { artifactId: string; nodeId: string; agentId: string; attempt: number; mediaType: string };
export type SequentialViewState = { graphRunId?: string; attempts: Record<string, SequentialAttempt>; reviews: Record<string, SequentialReview>; artifacts: Record<string, SequentialArtifact>; warnings: Record<string, { nodeId: string; attempt: number; code: string }>; interrupted?: { nodeId: string; attempt: number }; commandId?: string };

export const emptySequentialState = (): SequentialViewState => ({ attempts: {}, reviews: {}, artifacts: {}, warnings: {} });

const text = (value: unknown): string => typeof value === "string" ? value : "";
const integer = (value: unknown): number => typeof value === "number" && Number.isInteger(value) ? value : 0;
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function reduceSequentialEvent(state: SequentialViewState, event: ConversationEvent): SequentialViewState {
  const value = event.value ?? {};
  const graphRunId = text(value.graph_run_id) || state.graphRunId;
  if (event.name === "orchestration.graph.queued") return { ...state, graphRunId, commandId: text(value.command_id) };
  if (event.name === "orchestration.node.progress") {
    const attempt: SequentialAttempt = { nodeId: text(value.node_id), agentId: text(value.agent_id), attempt: integer(value.attempt), stage: text(value.stage), status: text(value.status), sequence: integer(value.sequence), ...(typeof value.percentage === "number" ? { percentage: value.percentage } : {}) };
    const key = `${attempt.nodeId}:${attempt.attempt}:${attempt.sequence}`;
    const previous = state.attempts[key];
    if (previous && JSON.stringify(previous) === JSON.stringify(attempt)) return state;
    return { ...state, graphRunId, attempts: { ...state.attempts, [key]: attempt } };
  }
  if (event.name === "orchestration.review.decided") {
    const review: SequentialReview = { reviewerNodeId: text(value.reviewer_node_id), reviewedNodeId: text(value.reviewed_node_id), reviewedAttempt: integer(value.reviewed_attempt), decision: text(value.decision) as SequentialReview["decision"], findings: strings(value.findings), evidenceRefs: strings(value.evidence_refs) };
    const key = `${review.reviewerNodeId}:${review.reviewedAttempt}`;
    return { ...state, graphRunId, reviews: { ...state.reviews, [key]: review } };
  }
  if (event.name === "orchestration.rework.requested") {
    const match = Object.entries(state.reviews).find(([, review]) => review.reviewedNodeId === value.reviewed_node_id && review.reviewedAttempt === value.reviewed_attempt && review.decision === "rejected");
    if (!match) return state;
    return { ...state, reviews: { ...state.reviews, [match[0]]: { ...match[1], reworkInstructions: text(value.rework_instructions) } } };
  }
  if (event.name === "orchestration.artifact.published") {
    const artifact: SequentialArtifact = { artifactId: text(value.artifact_id), nodeId: text(value.node_id), agentId: text(value.agent_id), attempt: integer(value.attempt), mediaType: text(value.media_type) };
    return { ...state, graphRunId, artifacts: { ...state.artifacts, [artifact.artifactId]: artifact } };
  }
  if (event.name === "orchestration.interrupted") return { ...state, graphRunId, interrupted: { nodeId: text(value.node_id), attempt: integer(value.attempt) } };
  if (event.name === "orchestration.warning") {
    const warning = { nodeId: text(value.node_id), attempt: integer(value.attempt), code: text(value.code) };
    return { ...state, graphRunId, warnings: { ...state.warnings, [`${warning.nodeId}:${warning.attempt}:${warning.code}`]: warning } };
  }
  return state;
}

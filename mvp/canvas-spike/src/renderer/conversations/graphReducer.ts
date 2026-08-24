import type { ConversationEvent } from "../api";

export type ResearchRecord = { branchId: string; attempt: number; stage: string; status: string; decision?: string; findings: string[]; evidenceRefs: string[] };
export type DevelopmentBranchRecord = { branchId: string; attempt: number; worktreeName: string; workerBranch: string; baseSha: string; commitSha: string; ownedPathSummary: string[]; testLabel: string; testResult: string; status: string; review?: string; findings: string[] };
export type DevelopmentGraphState = { graphRunId?: string; interruptId?: string; interruptKind?: string; status?: string; branches: Record<string, DevelopmentBranchRecord>; merge?: Record<string, unknown>; regression?: Record<string, unknown> };
export type ResearchGraphState = { graphRunId?: string; interruptId?: string; interruptKind?: string; records: Record<string, ResearchRecord>; supervisor?: Record<string, unknown>; arbitration?: Record<string, unknown>; merge?: Record<string, unknown>; globalReview?: Record<string, unknown>; development?: DevelopmentGraphState };

export const emptyResearchGraphState = (): ResearchGraphState => ({ records: {} });
const text = (value: unknown) => typeof value === "string" ? value : "";
const number = (value: unknown) => typeof value === "number" && Number.isInteger(value) ? value : 0;
const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const developmentState = (state: ResearchGraphState): DevelopmentGraphState => state.development ?? { branches: {} };

function reduceDevelopmentEvent(state: ResearchGraphState, event: ConversationEvent): ResearchGraphState | null {
  if (!event.name?.startsWith("development.")) return null;
  const value = event.value ?? {};
  const current = developmentState(state);
  const graphRunId = text(value.graph_run_id) || current.graphRunId;
  if (event.name === "development.plan.approved") return { ...state, development: { graphRunId, branches: {} } };
  if (event.name === "development.branch.progress") {
    const record: DevelopmentBranchRecord = { branchId: text(value.branch_id), attempt: number(value.attempt), worktreeName: text(value.worktree_display_name) || text(value.worktree_name), workerBranch: text(value.worker_branch), baseSha: text(value.base_sha), commitSha: text(value.commit_sha), ownedPathSummary: strings(value.owned_path_summary), testLabel: text(value.test_label), testResult: text(value.test_result), status: text(value.status), findings: [] };
    const key = `${record.branchId}:${record.attempt}`;
    return { ...state, development: { ...current, graphRunId, branches: { ...current.branches, [key]: record } } };
  }
  if (event.name === "development.local_review.decided") {
    const branchId = text(value.branch_id);
    const attempt = number(value.attempt);
    const key = `${branchId}:${attempt}`;
    const existing = current.branches[key];
    return { ...state, development: { ...current, graphRunId, branches: existing ? { ...current.branches, [key]: { ...existing, review: text(value.decision), findings: strings(value.findings) } } : current.branches } };
  }
  if (event.name === "development.merge.completed") return { ...state, development: { ...current, graphRunId, merge: value } };
  if (event.name === "development.global_verification.decided") return { ...state, development: { ...current, graphRunId, regression: value } };
  if (event.name === "development.interrupt.required") return { ...state, development: { ...current, graphRunId, interruptId: text(value.interrupt_id), interruptKind: text(value.interrupt_kind), status: text(value.status) } };
  return state;
}

export function reduceResearchEvent(state: ResearchGraphState, event: ConversationEvent): ResearchGraphState {
  const development = reduceDevelopmentEvent(state, event);
  if (development) return development;
  const value = event.value ?? {};
  const graphRunId = text(value.graph_run_id) || state.graphRunId;
  if (event.name === "research.plan.approved") {
    return graphRunId && graphRunId !== state.graphRunId
      ? { graphRunId, records: {} }
      : { ...state, graphRunId };
  }
  if (event.name === "research.branch.progress" || event.name === "research.local_review.decided") {
    const record: ResearchRecord = { branchId: text(value.branch_id), attempt: number(value.attempt), stage: text(value.stage), status: text(value.status || value.decision), ...(value.decision ? { decision: text(value.decision) } : {}), findings: strings(value.findings), evidenceRefs: strings(value.evidence_refs) };
    const key = `${record.branchId}:${record.attempt}:${record.stage}`;
    return { ...state, graphRunId, records: { ...state.records, [key]: record } };
  }
  if (event.name === "research.supervisor.decided") return { ...state, graphRunId, supervisor: value };
  if (event.name === "research.arbitration.decided") return { ...state, graphRunId, arbitration: value };
  if (event.name === "research.interrupt.required") return { ...state, graphRunId, interruptId: text(value.interrupt_id), interruptKind: text(value.interrupt_kind) };
  if (event.name === "research.merge.completed") return { ...state, graphRunId, interruptId: undefined, interruptKind: undefined, merge: value };
  if (event.name === "research.global_review.decided") return { ...state, graphRunId, interruptId: undefined, interruptKind: undefined, globalReview: value };
  if (event.name === "research.run.completed" || event.name === "research.run.failed") return { ...state, graphRunId, interruptId: undefined, interruptKind: undefined };
  return state;
}

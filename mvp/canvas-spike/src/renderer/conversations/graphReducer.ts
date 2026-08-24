import type { ConversationEvent } from "../api";

export type ResearchRecord = { branchId: string; attempt: number; stage: string; status: string; decision?: string; findings: string[]; evidenceRefs: string[] };
export type ResearchGraphState = { graphRunId?: string; records: Record<string, ResearchRecord>; supervisor?: Record<string, unknown>; arbitration?: Record<string, unknown>; merge?: Record<string, unknown>; globalReview?: Record<string, unknown> };

export const emptyResearchGraphState = (): ResearchGraphState => ({ records: {} });
const text = (value: unknown) => typeof value === "string" ? value : "";
const number = (value: unknown) => typeof value === "number" && Number.isInteger(value) ? value : 0;
const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function reduceResearchEvent(state: ResearchGraphState, event: ConversationEvent): ResearchGraphState {
  const value = event.value ?? {};
  const graphRunId = text(value.graph_run_id) || state.graphRunId;
  if (event.name === "research.branch.progress" || event.name === "research.local_review.decided") {
    const record: ResearchRecord = { branchId: text(value.branch_id), attempt: number(value.attempt), stage: text(value.stage), status: text(value.status || value.decision), ...(value.decision ? { decision: text(value.decision) } : {}), findings: strings(value.findings), evidenceRefs: strings(value.evidence_refs) };
    const key = `${record.branchId}:${record.attempt}:${record.stage}`;
    return { ...state, graphRunId, records: { ...state.records, [key]: record } };
  }
  if (event.name === "research.supervisor.decided") return { ...state, graphRunId, supervisor: value };
  if (event.name === "research.arbitration.decided") return { ...state, graphRunId, arbitration: value };
  if (event.name === "research.merge.completed") return { ...state, graphRunId, merge: value };
  if (event.name === "research.global_review.decided") return { ...state, graphRunId, globalReview: value };
  return state;
}

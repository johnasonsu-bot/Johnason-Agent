import { runWithConversationRetry } from "./conversations/retry";

export type VaultStatus = "uninitialized" | "locked" | "unlocked" | "recovery_required";

export interface ProviderProfile {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  headers: Record<string, string>;
  model_aliases: Record<string, string>;
  capabilities: string[];
  enabled: boolean;
  thinking_enabled: boolean;
  reasoning_effort: "high" | "max";
  credential_status: "configured" | "locked" | "missing" | "not_required";
}

export interface ProviderInput {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  model_aliases: Record<string, string>;
  capabilities?: string[];
  enabled?: boolean;
  thinking_enabled?: boolean;
  reasoning_effort?: "high" | "max";
}

export interface ConnectionResult {
  status: "online" | "offline" | "locked" | "missing" | "authentication_failed" | "error";
  latency_ms?: number;
  models: string[];
  error_code: string | null;
}

interface ApiBridge { apiRequest(request: { method: string; path: string; body?: Record<string, unknown>; headers?: Record<string, string> }): Promise<{ status: number; body: unknown; text?: string }>; }

export class ApiRequestError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "ApiRequestError";
  }
}

async function request<T>(path: string, init?: { method?: string; body?: string; headers?: Record<string, string> }): Promise<T> {
  let response: { status: number; body: unknown };
  try {
    const parsedBody = init?.body ? JSON.parse(init.body) as Record<string, unknown> : undefined;
    response = await (window as Window & { workbenchBridge: ApiBridge }).workbenchBridge.apiRequest({ method: init?.method ?? "GET", path, body: parsedBody, headers: init?.headers });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(message && message !== "Error" ? message : "无法连接到本地 Hermes 服务");
  }
  if (response.status < 200 || response.status >= 300) {
    const body = response.body as { detail?: unknown; message?: unknown; error?: unknown } | null;
    const detail = body && typeof body === "object"
      ? body.detail ?? body.message ?? body.error
      : undefined;
    const suffix = typeof detail === "string" && detail.length > 0 ? `：${detail}` : "";
    throw new ApiRequestError(`本地服务请求失败（${response.status}）${suffix}`, response.status);
  }
  return response.body as T;
}

export const providerApi = {
  vaultStatus: () => request<{ status: VaultStatus }>("/vault/status"),
  createVault: (password: string) => request<{ status: VaultStatus }>("/vault/create", {
    method: "POST", body: JSON.stringify({ password }),
  }),
  unlockVault: (password: string) => request<{ status: VaultStatus }>("/vault/unlock", {
    method: "POST", body: JSON.stringify({ password }),
  }),
  recoverVault: (password: string) => request<{ status: VaultStatus }>("/vault/recover", {
    method: "POST", body: JSON.stringify({ password }),
  }),
  lockVault: () => request<{ status: VaultStatus }>("/vault/lock", { method: "POST" }),
  listProviders: () => request<ProviderProfile[]>("/providers"),
  saveProvider: (provider: ProviderInput) => request<ProviderProfile>("/providers", {
    method: "POST", body: JSON.stringify(provider),
  }),
  saveSecret: (id: string, value: string) => request<{ credential_status: string }>(`/providers/${encodeURIComponent(id)}/secret`, {
    method: "POST", body: JSON.stringify({ value }),
  }),
  models: (id: string) => request<{ status: ConnectionResult["status"]; models: string[]; error_code: string | null }>(`/providers/${encodeURIComponent(id)}/models`),
  test: (id: string) => request<ConnectionResult>(`/providers/${encodeURIComponent(id)}/test`, { method: "POST" }),
  delete: (id: string) => request<{ id: string; status: string; secret_cleanup: string }>(`/providers/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

export type EngineHostStatus = {
  enabled: boolean;
  state: "disabled" | "starting" | "ready" | "degraded" | "unavailable";
  protocol: string | null;
  capabilities: null | {
    model: boolean;
    tools: boolean;
    skills: boolean;
    workspace: boolean;
    agui: boolean;
    max_frame_bytes: number;
  };
  runner_mode: "python" | "engine_host";
};

export const engineHostApi = {
  status: () => request<EngineHostStatus>("/engine-host/status"),
};

export type ConversationEvent = { type?: string; name?: string; delta?: string; result?: string; toolCallName?: string; value?: Record<string, unknown>; runId?: string; sequence?: number; eventId?: string; cursor?: string };

export type AgentProfileRecord = { agent_id: string; display_name: string; role: "worker" | "supervisor" | "verifier"; provider_id: string; model: string; enabled: boolean; tool_ids: string[]; skill_refs: string[]; version: number; created_at: number };
export type AgentProfileInput = Omit<AgentProfileRecord, "version" | "created_at">;

export const agentApi = {
  list: () => request<AgentProfileRecord[]>("/agents"),
  create: (profile: AgentProfileInput) => request<AgentProfileRecord>("/agents", { method: "POST", body: JSON.stringify(profile) }),
  replace: (profile: AgentProfileInput, expectedVersion: number) => request<AgentProfileRecord>(`/agents/${encodeURIComponent(profile.agent_id)}`, { method: "PUT", body: JSON.stringify({ ...profile, expected_version: expectedVersion }) }),
};

export type ArtifactContent = { artifact_id: string; media_type: string; content: string; digest: string };
export const artifactApi = {
  read: (artifactId: string) => request<ArtifactContent>(`/artifacts/${encodeURIComponent(artifactId)}`),
};

export type AgentBinding = { agent_id: string; expected_version: number };
export type ConversationResponse = { session_id: string; command_id: string; status: string; cursor?: string | null; events?: ConversationEvent[]; plan_id?: string; graph_run_id?: string };

const sendMessage = (sessionId: string, content: string, commandId: string, model = "default", providerId?: string, agentBindings: AgentBinding[] = []) => request<ConversationResponse>(`/sessions/${encodeURIComponent(sessionId)}/messages`, { method: "POST", body: JSON.stringify(agentBindings.length ? { content, agent_bindings: agentBindings } : { content, model, provider_id: providerId }), headers: { "Idempotency-Key": commandId } });

export function isRetryableConversationError(error: unknown): boolean {
  if (!(error instanceof ApiRequestError) || error.status !== 503) return false;
  const message = error.message.toLowerCase();
  // A local generation timeout is not a safe transient failure: retrying it
  // starts another expensive inference while the user still needs the detail.
  if (message.includes("readtimeout") || message.includes("timed out")) return false;
  return message.includes("agent turn is retryable");
}

export const conversationApi = {
  createSession: (sessionId: string) => request<{ session_id: string }>("/sessions", { method: "POST", body: JSON.stringify({ session_id: sessionId }) }),
  sendMessage,
  sendMessageWithRetry: (sessionId: string, content: string, commandId: string, model = "default", providerId?: string) => runWithConversationRetry(
    () => sendMessage(sessionId, content, commandId, model, providerId),
    isRetryableConversationError,
  ),
  events: async (sessionId: string, lastEventId?: string): Promise<ConversationEvent[]> => {
    const bridge = (window as Window & { workbenchBridge: ApiBridge }).workbenchBridge;
    const response = await bridge.apiRequest({ method: "GET", path: `/sessions/${encodeURIComponent(sessionId)}/events`, headers: lastEventId ? { "Last-Event-ID": lastEventId } : undefined });
    if (response.status < 200 || response.status >= 300) {
      const body = response.body as { detail?: unknown; message?: unknown; error?: unknown } | null;
      const detail = body && typeof body === "object" ? body.detail ?? body.message ?? body.error : undefined;
      const suffix = typeof detail === "string" && detail.length > 0 ? `：${detail}` : "";
      throw new Error(`本地服务请求失败（${response.status}）${suffix}`);
    }
    return (response.text ?? "").split("\n\n").filter(Boolean).map((frame) => {
      const cursor = frame.split("\n").find((line) => line.startsWith("id: "))?.slice(4);
      const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
      return data ? { ...(JSON.parse(data) as ConversationEvent), ...(cursor ? { cursor } : {}) } : null;
    }).filter((event): event is ConversationEvent => event !== null);
  },
  intervene: (sessionId: string, content: string, commandId: string) => request<{ status: string }>(`/sessions/${encodeURIComponent(sessionId)}/interventions`, {
    method: "POST", body: JSON.stringify({ kind: "supplement", content, context_version: 0 }), headers: { "Idempotency-Key": commandId },
  }),
  pause: (sessionId: string, commandId: string) => request<{ status: string }>(`/sessions/${encodeURIComponent(sessionId)}/pause`, { method: "POST", headers: { "Idempotency-Key": commandId } }),
  resume: (sessionId: string, commandId: string) => request<{ status: string }>(`/sessions/${encodeURIComponent(sessionId)}/resume`, { method: "POST", headers: { "Idempotency-Key": commandId } }),
  resumeOrchestration: (sessionId: string, targetCommandId: string, commandId: string) => request<{ status: string }>(`/sessions/${encodeURIComponent(sessionId)}/orchestrations/${encodeURIComponent(targetCommandId)}/resume`, { method: "POST", body: JSON.stringify({ decision: "approved" }), headers: { "Idempotency-Key": commandId } }),
  resumeDevelopmentInterrupt: (sessionId: string, graphRunId: string, interruptId: string, commandId: string) => request<{ graph_run_id: string; interrupt_id: string; status: string }>(`/sessions/${encodeURIComponent(sessionId)}/development-runs/${encodeURIComponent(graphRunId)}/interrupts/${encodeURIComponent(interruptId)}`, { method: "POST", body: JSON.stringify({ decision: "approved" }), headers: { "Idempotency-Key": commandId } }),
};

export type ResearchPlanNode = { node_id: string; kind: string; semantic_role: string; agent_id: string; display_name: string; agent_origin: "configured" | "temporary_proposal"; provider_id: string; model: string; tool_ids: string[]; skill_refs: string[] };
export type ResearchPlan = { plan_id: string; version: number; status: "draft" | "approved" | "queued"; goal: string; graph_run_id: string | null; parallel_worker_count: number; max_concurrency: number; temporary_agents: string[]; nodes: ResearchPlanNode[]; edges: Array<{ source_node_id: string; target_node_id: string; kind: string }>; artifact_contract: { media_type: string; required_sections: string[] }; diff?: { changed_roles: string[] } };

export const graphPlanApi = {
  propose: (sessionId: string, goal: string, commandId: string) => request<ResearchPlan>(`/sessions/${encodeURIComponent(sessionId)}/plans`, {
    method: "POST",
    headers: { "Idempotency-Key": commandId },
    body: JSON.stringify({ goal, source: "planner", source_refs: ["artifact:public-research-input"], max_concurrency: 4 }),
  }),
  get: (sessionId: string, planId: string, version: number) => request<ResearchPlan>(`/sessions/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(planId)}/versions/${version}`),
  approve: (sessionId: string, planId: string, version: number, commandId: string) => request<ResearchPlan>(`/sessions/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(planId)}/versions/${version}/approve`, {
    method: "POST",
    headers: { "Idempotency-Key": commandId },
    body: JSON.stringify({ actor_id: "local-user" }),
  }),
  replan: (sessionId: string, planId: string, version: number, reason: string, roles: string[], commandId: string) => request<ResearchPlan>(`/sessions/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(planId)}/versions/${version}/replan`, {
    method: "POST",
    headers: { "Idempotency-Key": commandId },
    body: JSON.stringify({ reason, affected_roles: roles }),
  }),
  resumeInterrupt: (graphRunId: string, interruptId: string, commandId: string, preference?: string) => request<{ graph_run_id: string; interrupt_id: string; status: string }>(`/graph-runs/${encodeURIComponent(graphRunId)}/interrupts/${encodeURIComponent(interruptId)}`, {
    method: "POST",
    headers: { "Idempotency-Key": commandId },
    body: JSON.stringify({ actor_id: "local-user", decision: "approved", ...(preference ? { preference } : {}) }),
  }),
};

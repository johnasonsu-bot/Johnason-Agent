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

async function request<T>(path: string, init?: { method?: string; body?: string; headers?: Record<string, string> }): Promise<T> {
  let response: { status: number; body: unknown };
  try {
    const parsedBody = init?.body ? JSON.parse(init.body) as Record<string, unknown> : undefined;
    response = await (window as Window & { workbenchBridge: ApiBridge }).workbenchBridge.apiRequest({ method: init?.method ?? "GET", path, body: parsedBody, headers: init?.headers });
  } catch {
    throw new Error("无法连接到本地 Hermes 服务");
  }
  if (response.status < 200 || response.status >= 300) throw new Error(`本地服务请求失败（${response.status}）`);
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

export type ConversationEvent = { type?: string; name?: string; delta?: string; value?: Record<string, unknown>; runId?: string; sequence?: number; eventId?: string };

export const conversationApi = {
  createSession: (sessionId: string) => request<{ session_id: string }>("/sessions", { method: "POST", body: JSON.stringify({ session_id: sessionId }) }),
  sendMessage: (sessionId: string, content: string, commandId: string) => request<{ session_id: string; status: string; events: ConversationEvent[] }>(`/sessions/${encodeURIComponent(sessionId)}/messages`, { method: "POST", body: JSON.stringify({ content, model: "default" }), headers: { "Idempotency-Key": commandId } }),
  events: async (sessionId: string): Promise<ConversationEvent[]> => {
    const bridge = (window as Window & { workbenchBridge: ApiBridge }).workbenchBridge;
    const response = await bridge.apiRequest({ method: "GET", path: `/sessions/${encodeURIComponent(sessionId)}/events` });
    if (response.status < 200 || response.status >= 300) throw new Error(`本地服务请求失败（${response.status}）`);
    return (response.text ?? "").split("\n\n").filter(Boolean).map((frame) => {
      const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
      return data ? JSON.parse(data) as ConversationEvent : null;
    }).filter((event): event is ConversationEvent => event !== null);
  },
};

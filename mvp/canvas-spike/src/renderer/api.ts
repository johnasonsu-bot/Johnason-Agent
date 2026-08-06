export type VaultStatus = "uninitialized" | "locked" | "unlocked";

export interface ProviderProfile {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  headers: Record<string, string>;
  model_aliases: Record<string, string>;
  capabilities: string[];
  thinking_enabled: boolean;
  reasoning_effort: "high" | "max";
  credential_status: "configured" | "locked" | "missing";
}

export interface ProviderInput {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  model_aliases: Record<string, string>;
  capabilities?: string[];
  thinking_enabled?: boolean;
  reasoning_effort?: "high" | "max";
}

export interface ConnectionResult {
  status: "online" | "offline" | "locked" | "missing" | "authentication_failed" | "error";
  latency_ms?: number;
  models: string[];
  error_code: string | null;
}

const API_BASE = "http://127.0.0.1:8765/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    throw new Error("无法连接到本地 Hermes 服务");
  }
  if (!response.ok) throw new Error(`本地服务请求失败（${response.status}）`);
  return response.json() as Promise<T>;
}

export const providerApi = {
  vaultStatus: () => request<{ status: VaultStatus }>("/vault/status"),
  createVault: (password: string) => request<{ status: VaultStatus }>("/vault/create", {
    method: "POST", body: JSON.stringify({ password }),
  }),
  unlockVault: (password: string) => request<{ status: VaultStatus }>("/vault/unlock", {
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
};

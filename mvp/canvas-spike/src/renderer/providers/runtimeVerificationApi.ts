export type VerificationStatus = "running" | "succeeded" | "failed" | "timed_out" | "cancelled";

export interface RuntimeVerification {
  id: string;
  status: VerificationStatus;
  runtime_id: "dsh";
  provider_profile_id: string;
  model: string;
}

interface VerificationBridge {
  apiRequest(request: { method: string; path: string; body?: Record<string, unknown> }): Promise<{ status: number; body: unknown }>;
}

export class VerificationRequestError extends Error {
  constructor(message: string, public readonly code: string) { super(message); }
}

const safeErrors: Record<string, string> = {
  invalid_verification_request: "验收输入无效，请检查供应商与 Vault 密码。",
  provider_incompatible: "该供应商不满足 DeepSeek Harness 验收条件，请先保存兼容配置。",
  provider_not_found: "已保存的供应商不存在，请刷新供应商配置。",
  verification_not_found: "找不到本次验收；本地服务可能已重启，无法确认结果。",
  verification_in_progress: "已有验收运行中，请等待其结束后再试。",
  verification_unavailable: "验收服务暂不可用，请检查本地运行环境后重试。",
};

async function request(path: string, method: string, body?: Record<string, unknown>): Promise<RuntimeVerification> {
  let response: { status: number; body: unknown };
  try {
    response = await (window as unknown as { workbenchBridge: VerificationBridge }).workbenchBridge.apiRequest({ method, path, body });
  } catch {
    // IPC exceptions may contain transport bodies. Never render those strings.
    throw new Error("无法连接本地验收服务，未确认验收结果。请检查服务状态。");
  }
  const value = response.body as Partial<RuntimeVerification> & { detail?: unknown } | null;
  if (response.status < 200 || response.status >= 300) {
    const code = value && typeof value.detail === "string" ? value.detail : "";
    throw new VerificationRequestError(safeErrors[code] ?? `验收请求失败（${response.status}），请检查本地服务。`, code);
  }
  if (!value || typeof value.id !== "string" || !/^[a-zA-Z0-9_-]+$/.test(value.id)
    || value.runtime_id !== "dsh" || typeof value.provider_profile_id !== "string" || typeof value.model !== "string"
    || !["running", "succeeded", "failed", "timed_out", "cancelled"].includes(value.status ?? "")) {
    throw new Error("验收服务返回了无法识别的状态，未确认验收结果。");
  }
  // Keep only the public contract. Raw subprocess output and server messages are
  // deliberately not retained in renderer state or displayed in the UI.
  return { id: value.id, status: value.status as VerificationStatus, runtime_id: "dsh", provider_profile_id: value.provider_profile_id, model: value.model };
}

export const runtimeVerificationApi = {
  start: (providerId: string, vaultPassword: string) => request("/runtime-verifications", "POST", { runtime_id: "dsh", provider_profile_id: providerId, vault_password: vaultPassword }),
  status: (id: string) => request(`/runtime-verifications/${encodeURIComponent(id)}`, "GET"),
  cancel: (id: string) => request(`/runtime-verifications/${encodeURIComponent(id)}/cancel`, "POST"),
};

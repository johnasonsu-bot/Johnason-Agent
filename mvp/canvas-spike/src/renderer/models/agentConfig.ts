export type ProviderId = "lmstudio" | "deepseek" | "openai_compatible" | "openai_chat";

export type AgentModelProfile = {
  id: string;
  name: string;
  role: string;
  providerId: ProviderId;
  model: string;
  enabled: boolean;
};

export const AGENT_MODEL_CONFIG_KEY = "hermes.v4.agent-model-config";

export const providerLabels: Record<ProviderId, string> = {
  lmstudio: "LM Studio",
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI Compatible",
  openai_chat: "OpenAI Chat",
};

export const defaultAgentModelProfiles: AgentModelProfile[] = [
  { id: "product-manager", name: "产品经理", role: "需求拆解与验收", providerId: "lmstudio", model: "local-agent", enabled: true },
  { id: "architect", name: "架构师", role: "方案设计与风险", providerId: "deepseek", model: "deepseek-v4-flash", enabled: true },
  { id: "engineer", name: "工程师", role: "实现与工具执行", providerId: "lmstudio", model: "local-agent", enabled: true },
  { id: "qa-engineer", name: "测试工程师", role: "验证与回归", providerId: "deepseek", model: "deepseek-v4-flash", enabled: true },
  { id: "agile-coach", name: "敏捷教练", role: "节奏与持续改进", providerId: "lmstudio", model: "local-agent", enabled: true },
  { id: "devops", name: "DevOps", role: "运行与发布", providerId: "openai_compatible", model: "gpt-4.1", enabled: true },
];

function isProfile(value: unknown): value is AgentModelProfile {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<AgentModelProfile>;
  return typeof item.id === "string"
    && typeof item.name === "string"
    && typeof item.role === "string"
    && typeof item.providerId === "string"
    && typeof item.model === "string"
    && typeof item.enabled === "boolean";
}

export function loadAgentModelProfiles(): AgentModelProfile[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(AGENT_MODEL_CONFIG_KEY) ?? "null") as unknown;
    if (!Array.isArray(parsed)) return defaultAgentModelProfiles.map((profile) => ({ ...profile }));
    const saved = parsed.filter(isProfile);
    return defaultAgentModelProfiles.map((profile) => saved.find((item) => item.id === profile.id) ?? { ...profile });
  } catch {
    return defaultAgentModelProfiles.map((profile) => ({ ...profile }));
  }
}

export function saveAgentModelProfiles(profiles: AgentModelProfile[]): void {
  localStorage.setItem(AGENT_MODEL_CONFIG_KEY, JSON.stringify(profiles));
}

export function agentModelProfileFor(profiles: AgentModelProfile[], name: string): AgentModelProfile {
  return profiles.find((profile) => profile.name === name) ?? profiles[0] ?? defaultAgentModelProfiles[0];
}

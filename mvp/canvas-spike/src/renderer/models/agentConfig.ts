import type { AgentProfileInput, AgentProfileRecord } from "../api";

export type ProviderId = string;
export type AgentRole = "worker" | "supervisor" | "verifier";

export type AgentModelProfile = {
  id: string;
  name: string;
  role: AgentRole;
  roleLabel: string;
  providerId: ProviderId;
  model: string;
  enabled: boolean;
  version: number;
  toolIds: string[];
  skillRefs: string[];
};

export const providerLabels: Record<string, string> = {
  lmstudio: "LM Studio",
  "deepseek-primary": "DeepSeek",
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI Compatible",
  openai_chat: "OpenAI Chat",
};

export const defaultAgentModelProfiles: AgentModelProfile[] = [
  { id: "product-manager", name: "产品经理", role: "worker", roleLabel: "需求拆解与内容创作", providerId: "lmstudio", model: "local-agent", enabled: true, version: 0, toolIds: [], skillRefs: [] },
  { id: "supervisor", name: "Supervisor", role: "supervisor", roleLabel: "审核与返工决策", providerId: "deepseek-primary", model: "deepseek-v4-flash", enabled: true, version: 0, toolIds: [], skillRefs: [] },
  { id: "architect", name: "架构师", role: "worker", roleLabel: "方案设计与 Artifact 生成", providerId: "deepseek-primary", model: "deepseek-v4-flash", enabled: true, version: 0, toolIds: [], skillRefs: [] },
  { id: "verifier", name: "Verifier", role: "verifier", roleLabel: "终局验证与证据检查", providerId: "deepseek-primary", model: "deepseek-v4-flash", enabled: true, version: 0, toolIds: [], skillRefs: [] },
  { id: "engineer", name: "工程师", role: "worker", roleLabel: "实现与工具执行", providerId: "lmstudio", model: "local-agent", enabled: true, version: 0, toolIds: [], skillRefs: [] },
  { id: "qa-engineer", name: "测试工程师", role: "verifier", roleLabel: "验证与回归", providerId: "deepseek-primary", model: "deepseek-v4-flash", enabled: true, version: 0, toolIds: [], skillRefs: [] },
];

export function fromAgentRecord(record: AgentProfileRecord): AgentModelProfile {
  const known = defaultAgentModelProfiles.find((profile) => profile.id === record.agent_id);
  return {
    id: record.agent_id,
    name: record.display_name,
    role: record.role,
    roleLabel: known?.roleLabel ?? record.role,
    providerId: record.provider_id,
    model: record.model,
    enabled: record.enabled,
    version: record.version,
    toolIds: record.tool_ids,
    skillRefs: record.skill_refs,
  };
}

export function mergeAgentRecords(records: AgentProfileRecord[]): AgentModelProfile[] {
  const persisted = records.map(fromAgentRecord);
  return [
    ...defaultAgentModelProfiles.map((fallback) => persisted.find((profile) => profile.id === fallback.id) ?? { ...fallback }),
    ...persisted.filter((profile) => !defaultAgentModelProfiles.some((fallback) => fallback.id === profile.id)),
  ];
}

export function toAgentInput(profile: AgentModelProfile): AgentProfileInput {
  return { agent_id: profile.id, display_name: profile.name, role: profile.role, provider_id: profile.providerId, model: profile.model, enabled: profile.enabled, tool_ids: profile.toolIds, skill_refs: profile.skillRefs };
}

export function loadAgentModelProfiles(): AgentModelProfile[] {
  return defaultAgentModelProfiles.map((profile) => ({ ...profile }));
}

export function agentModelProfileFor(profiles: AgentModelProfile[], name: string): AgentModelProfile {
  return profiles.find((profile) => profile.name === name) ?? profiles[0] ?? defaultAgentModelProfiles[0];
}

export function orderedMentionBindings(content: string, profiles: AgentModelProfile[]): Array<{ agent_id: string; expected_version: number }> {
  const available = [...profiles].filter((profile) => profile.enabled && profile.version > 0).sort((left, right) => right.name.length - left.name.length);
  const nonAgentMentions = new Set(["deep-research", "workspace.run"]);
  return [...content.matchAll(/@([^\s@]+)/g)].map((mention) => {
    const token = mention[1].replace(/[，,。.:：;；!?！？]+$/, "");
    if (nonAgentMentions.has(token)) return null;
    const profile = available.find((candidate) => candidate.name.toLocaleLowerCase() === token.toLocaleLowerCase());
    if (!profile) throw new Error(`未知或未启用的 Agent：@${token}`);
    return { agent_id: profile.id, expected_version: profile.version };
  }).filter((binding): binding is { agent_id: string; expected_version: number } => binding !== null);
}

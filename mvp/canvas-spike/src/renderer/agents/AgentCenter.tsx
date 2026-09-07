import { useEffect, useState } from "react";
import { agentApi, runtimeLabels, type RuntimeSelector } from "../api";
import {
  defaultAgentModelProfiles,
  loadAgentModelProfiles,
  providerLabels,
  fromAgentRecord,
  mergeAgentRecords,
  toAgentInput,
  type AgentModelProfile,
  type ProviderId,
} from "../models/agentConfig";
import { EngineHostStatus } from "./EngineHostStatus";

const providerOptions: ProviderId[] = ["lmstudio", "deepseek-primary", "deepseek", "openai_compatible", "openai_chat"];
const runtimeOptions: RuntimeSelector[] = ["python-term", "goose", "dsh"];

export function AgentCenter() {
  const [profiles, setProfiles] = useState<AgentModelProfile[]>(loadAgentModelProfiles);
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    void agentApi.list().then((records) => {
      if (active) setProfiles(mergeAgentRecords(records));
    }).catch((error: unknown) => {
      if (active) setMessage(error instanceof Error ? error.message : "Agent 配置加载失败");
    });
    return () => { active = false; };
  }, []);

  const update = (id: string, patch: Partial<AgentModelProfile>) => {
    setProfiles((current) => current.map((profile) => profile.id === id ? { ...profile, ...patch } : profile));
    setMessage("");
  };

  const save = async () => {
    const missingRuntime = profiles.find((profile) => profile.enabled && profile.runtimeId === null);
    if (missingRuntime) {
      setMessage(`Agent 配置保存失败：请为${missingRuntime.name}明确选择运行模式`);
      return;
    }
    try {
      const records = await Promise.all(profiles.map((profile) => profile.version > 0
        ? agentApi.replace(toAgentInput(profile), profile.version)
        : agentApi.create(toAgentInput(profile))));
      setProfiles(records.map(fromAgentRecord));
      setMessage("Agent 配置已保存");
    } catch (error) {
      setMessage(`Agent 配置保存失败：${error instanceof Error ? error.message : "请先配置并启用模型供应商"}`);
    }
  };

  return <section className="agent-center" aria-labelledby="agent-center-title">
    <header className="agent-center-heading">
      <div><p className="eyebrow">Cross-model routing · Batch2</p><h1 id="agent-center-title">Agent 配置 · Agent routing</h1><p>为每个 Agent 保存 Provider、模型与运行模式配置。</p></div>
      <button type="button" className="quiet" onClick={() => { setProfiles((current) => defaultAgentModelProfiles.map((profile) => ({ ...profile, version: current.find((item) => item.id === profile.id)?.version ?? 0 }))); setMessage("已载入默认值，保存后生效"); }}>恢复默认</button>
    </header>
    {message && <p role="status" className="notice">{message}</p>}
    <EngineHostStatus />
    <div className="agent-center-note"><strong>配置说明</strong><span>运行模式仅作为已保存配置，尚未接入节点执行；接线将在 R4 集成后生效。Provider 需要先配置并启用；此处不保存 API Key。</span></div>
    <div className="agent-config-grid">{profiles.map((profile) => <article className="agent-config-card" key={profile.id}>
      <div className="agent-config-card-heading"><span className="agent-avatar agent-avatar-blue">{profile.name.slice(0, 1)}</span><div><h2>{profile.name}</h2><p>{profile.roleLabel} · v{profile.version || "new"}</p></div><label className="agent-enabled"><input type="checkbox" checked={profile.enabled} onChange={(event) => update(profile.id, { enabled: event.target.checked })} />启用</label></div>
      <label>{profile.name} Provider<select aria-label={`${profile.name} Provider`} value={profile.providerId} onChange={(event) => update(profile.id, { providerId: event.target.value as ProviderId })}>{providerOptions.map((provider) => <option key={provider} value={provider}>{providerLabels[provider]} · {provider}</option>)}</select></label>
      <label>{profile.name} Model<input aria-label={`${profile.name} Model`} value={profile.model} onChange={(event) => update(profile.id, { model: event.target.value })} placeholder="例如 deepseek-v4-flash" /></label>
      <label>{profile.name} 运行模式<select aria-label={`${profile.name} 运行模式`} value={profile.runtimeId ?? ""} onChange={(event) => update(profile.id, { runtimeId: event.target.value as RuntimeSelector })}><option value="" disabled>请选择运行模式</option>{runtimeOptions.map((runtime) => <option key={runtime} value={runtime}>{runtimeLabels[runtime]}</option>)}</select></label>
      <small className="agent-config-route">配置绑定：{providerLabels[profile.providerId]} / {profile.model} / {profile.runtimeId ? runtimeLabels[profile.runtimeId] : "未指定运行模式"}</small>
    </article>)}</div>
    <footer className="agent-center-actions"><button type="button" onClick={() => void save()}>保存 Agent 配置</button></footer>
  </section>;
}

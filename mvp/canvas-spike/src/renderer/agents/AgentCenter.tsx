import { useState } from "react";
import {
  defaultAgentModelProfiles,
  loadAgentModelProfiles,
  providerLabels,
  saveAgentModelProfiles,
  type AgentModelProfile,
  type ProviderId,
} from "../models/agentConfig";
import { EngineHostStatus } from "./EngineHostStatus";

const providerOptions: ProviderId[] = ["lmstudio", "deepseek", "openai_compatible", "openai_chat"];

export function AgentCenter() {
  const [profiles, setProfiles] = useState<AgentModelProfile[]>(loadAgentModelProfiles);
  const [message, setMessage] = useState("");

  const update = (id: string, patch: Partial<AgentModelProfile>) => {
    setProfiles((current) => current.map((profile) => profile.id === id ? { ...profile, ...patch } : profile));
    setMessage("");
  };

  const save = () => {
    saveAgentModelProfiles(profiles);
    setMessage("Agent 配置已保存");
  };

  return <section className="agent-center" aria-labelledby="agent-center-title">
    <header className="agent-center-heading">
      <div><p className="eyebrow">Cross-model routing · Batch2</p><h1 id="agent-center-title">Agent 配置 · Agent routing</h1><p>为每个 Agent 选择 Provider 和模型，用于单 Agent 或多 Agent 跨模型会话测试。</p></div>
      <button type="button" className="quiet" onClick={() => { setProfiles(defaultAgentModelProfiles.map((profile) => ({ ...profile }))); setMessage("已恢复默认配置"); }}>恢复默认</button>
    </header>
    {message && <p role="status" className="notice">{message}</p>}
    <EngineHostStatus />
    <div className="agent-center-note"><strong>路由说明</strong><span>Provider 需要先在“模型供应商”中配置并启用；此处只保存 provider/model 标识，不保存 API Key。</span></div>
    <div className="agent-config-grid">{profiles.map((profile) => <article className="agent-config-card" key={profile.id}>
      <div className="agent-config-card-heading"><span className="agent-avatar agent-avatar-blue">{profile.name.slice(0, 1)}</span><div><h2>{profile.name}</h2><p>{profile.role}</p></div><label className="agent-enabled"><input type="checkbox" checked={profile.enabled} onChange={(event) => update(profile.id, { enabled: event.target.checked })} />启用</label></div>
      <label>{profile.name} Provider<select aria-label={`${profile.name} Provider`} value={profile.providerId} onChange={(event) => update(profile.id, { providerId: event.target.value as ProviderId })}>{providerOptions.map((provider) => <option key={provider} value={provider}>{providerLabels[provider]} · {provider}</option>)}</select></label>
      <label>{profile.name} Model<input aria-label={`${profile.name} Model`} value={profile.model} onChange={(event) => update(profile.id, { model: event.target.value })} placeholder="例如 deepseek-v4-flash" /></label>
      <small className="agent-config-route">当前路由：{providerLabels[profile.providerId]} / {profile.model}</small>
    </article>)}</div>
    <footer className="agent-center-actions"><button type="button" onClick={save}>保存 Agent 配置</button></footer>
  </section>;
}

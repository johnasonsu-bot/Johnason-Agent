import { FormEvent, useCallback, useEffect, useState } from "react";
import { providerApi, type ConnectionResult, type ProviderProfile, type VaultStatus } from "../api";
import { ProviderForm, type ProviderDraft } from "./ProviderForm";

const statusCopy: Record<ConnectionResult["status"], string> = {
  online: "连接正常", offline: "服务离线", locked: "保险库已锁定", missing: "缺少凭据", authentication_failed: "身份验证失败", error: "连接错误",
};

const credentialCopy: Record<ProviderProfile["credential_status"], string> = {
  configured: "凭据已配置", locked: "保险库已锁定", missing: "未配置凭据", not_required: "无需凭据",
};

function asInput(draft: ProviderDraft, defaultModel?: string) {
  return {
    id: draft.id,
    name: draft.name,
    protocol: draft.protocol,
    credential_mode: draft.credential_mode ?? "reference",
    base_url: draft.base_url,
    model_aliases: defaultModel ? { ...draft.model_aliases, default: defaultModel } : draft.model_aliases,
    capabilities: draft.capabilities,
    enabled: draft.enabled,
    thinking_enabled: draft.thinking_enabled,
    reasoning_effort: draft.reasoning_effort,
  };
}

export function ProviderCenter() {
  const [vaultStatus, setVaultStatus] = useState<VaultStatus>("locked");
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("正在连接本地服务…");
  const [connection, setConnection] = useState<ConnectionResult | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [locking, setLocking] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const vault = await providerApi.vaultStatus();
      setVaultStatus(vault.status);
      if (vault.status === "unlocked") {
        const listed = await providerApi.listProviders();
        setProviders(listed);
        setSelectedId((current) => current ?? listed[0]?.id ?? null);
      }
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法连接到本地 Hermes 服务");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const selected = providers.find((provider) => provider.id === selectedId) ?? null;

  const unlock = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const submittedPassword = password;
    event.currentTarget.reset();
    setPassword("");
    try {
      const result = vaultStatus === "uninitialized"
        ? await providerApi.createVault(submittedPassword)
        : vaultStatus === "recovery_required"
          ? await providerApi.recoverVault(submittedPassword)
          : await providerApi.unlockVault(submittedPassword);
      setVaultStatus(result.status);
      setMessage("");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法解锁保险库");
    }
  };

  const save = async (draft: ProviderDraft) => {
    try {
      const saved = await providerApi.saveProvider(asInput(draft));
      if (draft.apiKey) await providerApi.saveSecret(saved.id, draft.apiKey);
      setSelectedId(saved.id);
      setMessage("供应商已保存");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法保存供应商");
    }
  };

  const test = async (draft: ProviderDraft) => {
    try {
      const saved = await providerApi.saveProvider(asInput(draft));
      if (draft.apiKey) await providerApi.saveSecret(saved.id, draft.apiKey);
      setSelectedId(saved.id);
      const result = await providerApi.test(saved.id);
      setConnection(result);
      setModels(result.models);
      setMessage("");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法测试连接");
    }
  };

  const selectModel = async (model: string) => {
    if (!selected) return;
    try {
      await providerApi.saveProvider({
        id: selected.id, name: selected.name, protocol: selected.protocol, base_url: selected.base_url,
        model_aliases: { ...selected.model_aliases, default: model }, capabilities: selected.capabilities,
        enabled: selected.enabled, thinking_enabled: selected.thinking_enabled, reasoning_effort: selected.reasoning_effort,
        credential_mode: selected.credential_mode ?? "reference",
      });
      setModels((current) => current.includes(model) ? current : [model, ...current]);
      await refresh();
      setMessage("默认模型已更新");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法更新默认模型");
    }
  };

  const discoverModels = async () => {
    if (!selected) return;
    try {
      const result = await providerApi.models(selected.id);
      setModels(result.models);
      setConnection({ status: result.status, models: result.models, error_code: result.error_code });
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法发现模型");
    }
  };

  const lock = async () => {
    setLocking(true);
    try {
      await providerApi.lockVault();
      setProviders([]);
      setSelectedId(null);
      setModels([]);
      setConnection(null);
      setVaultStatus("locked");
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法锁定保险库");
    } finally {
      setLocking(false);
    }
  };

  const removeSelected = async () => {
    if (!selected || !window.confirm(`删除供应商“${selected.name}”？此操作会移除其本地凭据引用。`)) return;
    setDeleting(true);
    try {
      const result = await providerApi.delete(selected.id);
      setProviders((current) => current.filter((provider) => provider.id !== selected.id));
      setSelectedId(null);
      setModels([]);
      setConnection(null);
      setMessage(result.secret_cleanup === "unconfirmed" ? "供应商元数据已删除；凭据删除耐久性未确认" : "供应商已删除");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法删除供应商");
    } finally {
      setDeleting(false);
    }
  };

  if (vaultStatus !== "unlocked") return (
    <section className="provider-center" aria-labelledby="provider-title">
      <p className="eyebrow">安全连接</p><h2 id="provider-title">模型供应商</h2>
      <p>凭据仅保存在本机加密保险库中，应用重启后默认锁定。</p>
      {vaultStatus === "recovery_required" && <p role="alert">保险库需要恢复；原始损坏文件会保留为本地恢复副本。</p>}
      <form className="vault-form" onSubmit={(event) => void unlock(event)}>
        <label>主密码<input aria-label="主密码" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        <button type="submit">{vaultStatus === "uninitialized" ? "创建并解锁" : vaultStatus === "recovery_required" ? "恢复并创建" : "解锁"}</button>
      </form>
      {message && <p role="status" className="notice">{message}</p>}
    </section>
  );

  const visibleModels = models.length ? models : selected ? Object.values(selected.model_aliases) : [];
  return (
    <section className="provider-center" aria-labelledby="provider-title">
      <div className="center-heading"><div><p className="eyebrow">本地连接</p><h2 id="provider-title">模型供应商</h2><p>管理本地和云端模型；保险库当前已解锁。</p></div><button type="button" className="quiet" disabled={locking} onClick={() => void lock()}>{locking ? "正在锁定…" : "锁定保险库"}</button></div>
      {message && <p role="status" className="notice">{message}</p>}
      <div className="provider-layout">
        <aside aria-label="已保存的供应商"><h3>供应商</h3>{providers.length === 0 ? <p className="empty">从预设开始连接一个模型。</p> : providers.map((provider) => <button aria-pressed={provider.id === selectedId} className={provider.id === selectedId ? "provider-item active" : "provider-item"} type="button" key={provider.id} onClick={() => { setSelectedId(provider.id); setModels([]); setConnection(null); }}><strong>{provider.name}</strong><span>{provider.enabled ? credentialCopy[provider.credential_status] : "已停用"}</span></button>)}</aside>
        <div className="provider-detail">
          <ProviderForm provider={selected} onSave={save} onTest={test} />
          {selected && <button type="button" className="quiet" disabled={!selected.enabled} onClick={() => void discoverModels()}>发现模型</button>}
          {selected && <button type="button" className="quiet danger" disabled={deleting} onClick={() => void removeSelected()}>{deleting ? "正在删除…" : "删除供应商"}</button>}
          {connection && <section className={`connection ${connection.status}`} aria-live="polite"><strong>{statusCopy[connection.status]}</strong>{connection.latency_ms !== undefined && <span>{connection.latency_ms} ms</span>}{connection.error_code && <span>{connection.error_code}</span>}</section>}
          {visibleModels.length > 0 && <label className="model-picker">默认模型<select aria-label="默认模型" value={selected?.model_aliases.default ?? visibleModels[0]} onChange={(event) => void selectModel(event.target.value)}>{visibleModels.map((model) => <option key={model} value={model}>{model}</option>)}</select></label>}
        </div>
      </div>
    </section>
  );
}

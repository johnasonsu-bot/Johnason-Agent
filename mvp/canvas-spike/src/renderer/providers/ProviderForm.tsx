import { useEffect, useState } from "react";
import type { ProviderInput, ProviderProfile } from "../api";

export interface ProviderDraft extends ProviderInput {
  apiKey: string;
}

export const presets: Record<"lmstudio" | "deepseek", ProviderDraft> = {
  lmstudio: {
    id: "lmstudio",
    name: "LM Studio",
    protocol: "lmstudio",
    credential_mode: "none",
    base_url: "http://127.0.0.1:1234",
    model_aliases: {},
    capabilities: ["streaming", "tool_calling"],
    enabled: true,
    thinking_enabled: false,
    reasoning_effort: "high",
    apiKey: "",
  },
  deepseek: {
    id: "deepseek-primary",
    name: "DeepSeek V4 Flash",
    protocol: "deepseek",
    credential_mode: "reference",
    base_url: "https://api.deepseek.com",
    model_aliases: { default: "deepseek-v4-flash" },
    capabilities: ["streaming", "tool_calling", "thinking"],
    enabled: true,
    thinking_enabled: true,
    reasoning_effort: "high",
    apiKey: "",
  },
};

function draftFrom(profile: ProviderProfile): ProviderDraft {
  return {
    id: profile.id,
    name: profile.name,
    protocol: profile.protocol,
    credential_mode: profile.credential_mode ?? "reference",
    base_url: profile.base_url,
    model_aliases: profile.model_aliases,
    capabilities: profile.capabilities,
    enabled: profile.enabled,
    thinking_enabled: profile.thinking_enabled,
    reasoning_effort: profile.reasoning_effort,
    apiKey: "",
  };
}

interface Props {
  provider: ProviderProfile | null;
  onSave: (draft: ProviderDraft) => Promise<void>;
  onTest: (draft: ProviderDraft) => Promise<void>;
}

export function ProviderForm({ provider, onSave, onTest }: Props) {
  const [draft, setDraft] = useState<ProviderDraft>(presets.lmstudio);
  const [busy, setBusy] = useState(false);

  useEffect(() => { setDraft(provider ? draftFrom(provider) : presets.lmstudio); }, [provider]);

  const update = <K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const submit = async (form: HTMLFormElement, action: "save" | "test") => {
    const submitted = draft;
    // Clear the DOM and React state before the request can resolve or be inspected.
    form.reset();
    setDraft((current) => ({ ...current, apiKey: "" }));
    setBusy(true);
    try {
      await (action === "test" ? onTest(submitted) : onSave(submitted));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="provider-form" onSubmit={(event) => { event.preventDefault(); void submit(event.currentTarget, "save"); }}>
      <div className="preset-row" aria-label="供应商预设">
        <button type="button" onClick={() => setDraft(presets.lmstudio)}>使用 LM Studio</button>
        <button type="button" onClick={() => setDraft(presets.deepseek)}>使用 DeepSeek</button>
      </div>
      <label>名称<input aria-label="供应商名称" value={draft.name} onChange={(event) => update("name", event.target.value)} required /></label>
      <label>基础地址<input aria-label="基础地址" type="url" value={draft.base_url} onChange={(event) => update("base_url", event.target.value)} required /></label>
      <label className="checkbox-row"><input aria-label="启用供应商" type="checkbox" checked={draft.enabled !== false} onChange={(event) => update("enabled", event.target.checked)} />启用供应商</label>
      {draft.protocol === "deepseek" && <label>推理强度<select aria-label="推理强度" value={draft.reasoning_effort ?? "high"} onChange={(event) => update("reasoning_effort", event.target.value as "high" | "max")}><option value="high">high</option><option value="max">max</option></select></label>}
      <label>API 密钥（仅写入加密保险库）<input aria-label="API 密钥" type="password" autoComplete="off" value={draft.apiKey} onChange={(event) => update("apiKey", event.target.value)} placeholder={draft.protocol === "lmstudio" ? "LM Studio 无需密钥" : "输入后立即加密保存"} /></label>
      <p className="hint">凭据不会显示、导出或保存到此设备的界面状态。</p>
      <div className="form-actions">
        <button type="submit" disabled={busy}>{busy ? "正在保存…" : "保存供应商"}</button>
        <button type="button" disabled={busy || draft.enabled === false} onClick={(event) => { if (event.currentTarget.form) void submit(event.currentTarget.form, "test"); }}>测试连接</button>
      </div>
    </form>
  );
}

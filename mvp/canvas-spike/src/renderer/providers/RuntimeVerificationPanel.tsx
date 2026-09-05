import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import type { ProviderProfile } from "../api";
import { runtimeVerificationApi, VerificationRequestError, type RuntimeVerification, type VerificationStatus } from "./runtimeVerificationApi";

type ViewState = { job: RuntimeVerification | null; generation: number; starting: boolean; cancelling: boolean; attempted: boolean; recordMissing: boolean; error: string };
// Job metadata survives route changes in memory only. Passwords never enter this
// store, browser storage, logs, or a saved provider profile.
let retained: ViewState = { job: null, generation: 0, starting: false, cancelling: false, attempted: false, recordMissing: false, error: "" };
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const snapshot = () => retained;
function update(patch: Partial<ViewState>) {
  retained = { ...retained, ...patch };
  listeners.forEach((listener) => listener());
}
const isCurrentJob = (generation: number, id: string) => retained.generation === generation && retained.job?.id === id;

const statusCopy: Record<VerificationStatus, string> = {
  running: "验收运行中", succeeded: "验收通过", failed: "验收失败", timed_out: "验收超时", cancelled: "验收已取消",
};
const resultCopy: Record<VerificationStatus, string> = {
  running: "后台正在进行独立外部验证，最长约 5 分钟。请求已受理不代表验收通过。",
  succeeded: "独立验证已完成；结果不会自动更新运行时准入。",
  failed: "独立验证未通过。请检查 Vault 密码、已保存凭据、模型及本地验收环境后重新验收。",
  timed_out: "已达到后台验收时限。请检查网络及本地运行环境后重新验收。",
  cancelled: "本次验收已停止；已产生的 API 费用不会撤销。",
};

function ineligible(provider: ProviderProfile | undefined): string {
  if (!provider) return "请先保存一个 DeepSeek 供应商。";
  if (!provider.enabled) return "该供应商已停用，请先启用并保存。";
  if (!provider.model_aliases.default?.trim()) return "该供应商缺少已保存的默认模型。";
  if ((provider.credential_mode ?? "reference") !== "reference" || provider.credential_status === "missing" || provider.credential_status === "not_required") return "请先在供应商配置中保存 API 密钥。";
  if (Object.keys(provider.headers).length) return "验收不支持自定义请求头，请先保存兼容配置。";
  return "";
}

export function RuntimeVerificationPanel({ providers, selectedProviderId }: { providers: ProviderProfile[]; selectedProviderId?: string }) {
  const state = useSyncExternalStore(subscribe, snapshot);
  const compatible = providers.filter((provider) => provider.protocol === "deepseek");
  const fallback = compatible.find((provider) => provider.id === selectedProviderId)?.id
    ?? compatible.find((provider) => provider.id === "deepseek-primary")?.id ?? compatible[0]?.id ?? "";
  const [providerId, setProviderId] = useState(fallback);
  const [password, setPassword] = useState("");
  const passwordInput = useRef<HTMLInputElement>(null);
  const selected = compatible.find((provider) => provider.id === providerId);
  const running = state.job?.status === "running" && !state.recordMissing;
  const busy = state.starting || state.cancelling || running;
  const problem = ineligible(selected);

  useEffect(() => {
    if (!compatible.some((provider) => provider.id === providerId)) setProviderId(fallback);
  }, [providers, providerId, fallback]);

  useEffect(() => {
    if (!state.job || state.job.status !== "running" || state.recordMissing) return;
    const id = state.job.id;
    const generation = state.generation;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const job = await runtimeVerificationApi.status(id);
        if (active && isCurrentJob(generation, id) && retained.job?.status === "running" && !retained.cancelling) update({ job, error: "" });
      } catch (error) {
        if (active && isCurrentJob(generation, id) && retained.job?.status === "running") {
          if (error instanceof VerificationRequestError && error.code === "verification_not_found") {
            // A missing record invalidates every outstanding operation on it,
            // including a cancellation response that can still arrive later.
            update({ generation: generation + 1, cancelling: false, recordMissing: true, error: "验收记录已失效，结果未知。本地服务可能已重启；请检查后重新验收。" });
          } else update({ error: "暂时无法读取验收状态；结果仍未确认，将继续查询。请勿重复启动。" });
        }
      }
      if (active && isCurrentJob(generation, id) && retained.job?.status === "running" && !retained.recordMissing) timer = setTimeout(() => void poll(), 1000);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [state.generation, state.job?.id, state.job?.status, state.recordMissing]);

  const start = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (retained.starting || retained.cancelling || (retained.job?.status === "running" && !retained.recordMissing) || problem || !password) return;
    const submittedPassword = password;
    if (passwordInput.current) passwordInput.current.value = "";
    setPassword("");
    const generation = retained.generation + 1;
    update({ generation, starting: true, cancelling: false, attempted: true, job: null, recordMissing: false, error: "" });
    try {
      const job = await runtimeVerificationApi.start(providerId, submittedPassword);
      if (retained.generation === generation && retained.starting) update({ job, starting: false });
    } catch (error) {
      if (retained.generation === generation && retained.starting) update({ starting: false, error: error instanceof Error ? error.message : "无法启动验收。" });
    }
  };

  const cancel = async () => {
    const job = retained.job;
    if (!job || job.status !== "running" || retained.recordMissing || retained.cancelling) return;
    const generation = retained.generation;
    update({ cancelling: true, error: "" });
    try {
      const result = await runtimeVerificationApi.cancel(job.id);
      if (isCurrentJob(generation, job.id)) update({ job: result, cancelling: false });
    } catch (error) {
      if (isCurrentJob(generation, job.id)) update({ cancelling: false, error: error instanceof Error ? error.message : "未确认取消结果，请重试。" });
    }
  };

  return <section className="connection" aria-labelledby="runtime-verification-title">
    <p className="eyebrow">Manual verification</p>
    <h3 id="runtime-verification-title">DeepSeek Harness 人工验收</h3>
    <p>运行时固定为 DeepSeek Harness（dsh）。测试连接不等于 Harness 验收。</p>
    <p>只有点击“开始真实验收”或“重新验收”才会发起云端调用，可能产生真实 API 费用。</p>
    <form className="provider-form" onSubmit={(event) => void start(event)}>
      <label>验收供应商<select aria-label="验收供应商" value={providerId} disabled={busy || compatible.length === 0} onChange={(event) => { setProviderId(event.target.value); setPassword(""); }}>
        {compatible.length === 0 && <option value="">暂无已保存的 DeepSeek 供应商</option>}
        {compatible.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}{provider.enabled ? "" : "（已停用）"}</option>)}
      </select></label>
      <label>已保存的验收模型<input aria-label="已保存的验收模型" value={selected?.model_aliases.default ?? ""} readOnly /></label>
      <p className="hint">使用已保存的默认模型。要改模型，请先在供应商配置中保存；未保存的表单修改不会用于验收。</p>
      <label>本次验收 Vault 密码<input ref={passwordInput} aria-label="本次验收 Vault 密码" type="password" autoComplete="off" value={password} disabled={busy} onChange={(event) => setPassword(event.target.value)} required /></label>
      <p className="hint">这是保险库密码，不是 API Key。仅用于本次后台验证，提交后立即清空；现有凭据只读，不会保存此密码。</p>
      {problem && <p className="hint">{problem}</p>}
      <div className="form-actions">
        <button type="submit" disabled={busy || Boolean(problem) || !password}>{state.starting ? "正在启动…" : running ? "验收进行中" : state.attempted ? "重新验收" : "开始真实验收"}</button>
        {running && <button type="button" className="quiet" disabled={state.cancelling} onClick={() => void cancel()}>{state.cancelling ? "正在取消…" : "取消验收"}</button>}
      </div>
    </form>
    {state.starting && <p role="status">正在提交验收请求，尚未确认启动。</p>}
    {state.job && <div role="status" aria-live="polite" className="notice">
      <strong>{state.recordMissing ? "结果未知" : statusCopy[state.job.status]}</strong>
      {!state.recordMissing && <p>{resultCopy[state.job.status]}</p>}
      <p>验收编号：{state.job.id} · 供应商：{state.job.provider_profile_id} · 模型：{state.job.model}</p>
    </div>}
    {state.error && <p role="alert">{state.error}</p>}
    <p className="hint">离开此页面不会取消验收；返回后继续查看本次状态，不会再次提交。关闭客户端后状态不保留。</p>
  </section>;
}

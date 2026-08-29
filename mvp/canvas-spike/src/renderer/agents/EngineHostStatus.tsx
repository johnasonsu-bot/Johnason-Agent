import { useCallback, useEffect, useState } from "react";
import {
  engineHostApi,
  type EngineHostStatus as EngineHostStatusValue,
  type EngineHostV2Diagnostic,
} from "../api";

const capabilityLabels: Record<keyof NonNullable<EngineHostStatusValue["capabilities"]>, string> = {
  model: "Model",
  tools: "Tools",
  skills: "Skills",
  workspace: "Workspace",
  agui: "AG-UI",
  max_frame_bytes: "1 MiB frames",
};

export function EngineHostStatus() {
  const [status, setStatus] = useState<EngineHostStatusValue | null>(null);
  const [v2Status, setV2Status] = useState<EngineHostV2Diagnostic | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const [v1, v2] = await Promise.all([engineHostApi.status(), engineHostApi.v2Status()]);
      setStatus(v1);
      setV2Status(v2);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Engine Host 状态");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const runner = status?.runner_mode === "engine_host" ? "Engine Host" : "Python Runtime";
  const capabilities = status?.capabilities
    ? Object.entries(status.capabilities).filter(([name, value]) => name === "max_frame_bytes" ? value === 1_048_576 : value === true)
    : [];

  return <section className="engine-host-status" aria-label="Engine Host 状态">
    <div className="engine-host-status-heading">
      <div>
        <p className="eyebrow">Runtime contract · G1</p>
        <h2>Engine Host 状态</h2>
      </div>
      <button type="button" className="quiet" disabled={refreshing} onClick={() => void refresh()} aria-label="刷新 Engine Host 状态">
        {refreshing ? "刷新中…" : "刷新"}
      </button>
    </div>
    {error ? <p className="engine-host-error" role="status">{error}</p> : <>
      <div className="engine-host-summary">
        <strong>{runner}</strong>
        <span className={`engine-host-state engine-host-state-${status?.state ?? "starting"}`}>{status?.state ?? "starting"}</span>
        <span>{status?.protocol ?? "Host 未启用"}</span>
      </div>
      {capabilities.length > 0 && <div className="engine-host-capabilities" aria-label="Engine Host 能力">
        {capabilities.map(([name]) => <span key={name}>{capabilityLabels[name as keyof typeof capabilityLabels]}</span>)}
      </div>}
      <div className="engine-host-capabilities" aria-label="Host v2 只读诊断">
        <span>{`Host v2 · ${v2Status?.v2.enabled ? "enabled" : "disabled"}`}</span>
        {v2Status?.v2.runtimes.map((runtime) => (
          <span key={`${runtime.runtime_id}:${runtime.build_id}`}>
            {`${runtime.runtime_id} · ${runtime.state}`}
          </span>
        ))}
      </div>
    </>}
    <p className="engine-host-readonly">只读诊断 · 不提供命令、环境变量或凭据编辑</p>
  </section>;
}

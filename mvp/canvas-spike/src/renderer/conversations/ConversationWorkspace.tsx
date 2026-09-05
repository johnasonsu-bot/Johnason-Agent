import { useEffect, useMemo, useRef, useState } from "react";
import { artifacts } from "../artifacts";
import { agentApi, conversationApi, engineHostApi, providerApi, runtimeLabels, isRuntimeSelector, type RuntimeSelector, type ProviderProfile, type ConversationEvent, type EngineHostV2Diagnostic } from "../api";
import { registry } from "../renderers";
import { Composer, type RuntimeOption } from "./Composer";
import { SessionSidebar } from "./SessionSidebar";
import { Timeline, type TimelineEntry } from "./Timeline";
import { createConversationCommandId } from "./commandIds";
import { agentModelProfileFor, loadAgentModelProfiles, mergeAgentRecords, orderedMentionBindings, providerLabels, type AgentModelProfile } from "../models/agentConfig";
import { HtmlArtifactPreview } from "./HtmlArtifactPreview";
import { SequentialGraph } from "./SequentialGraph";
import { emptySequentialState, reduceSequentialEvent } from "./sequentialReducer";
import { PlanApproval } from "./PlanApproval";
import { GraphRun } from "./GraphRun";
import { emptyResearchGraphState, reduceResearchEvent } from "./graphReducer";

const seededTitles: Record<string, string> = {
  "ui-session-0": "Jira 看板配置修复指引",
  "ui-session-1": "方案评审 · Architecture review",
  "ui-session-2": "本地文件整理",
  "ui-session-3": "数据平台任务调试",
};
const seededGroups: Record<string, string[]> = {
  "ui-session-0": ["产品经理", "架构师", "工程师"],
  "ui-session-1": ["架构师"],
  "ui-session-2": ["工程师"],
  "ui-session-3": ["产品经理", "工程师"],
};
const initialTimeline: TimelineEntry[] = [
  { id: "initial-user", kind: "user", title: "你", content: "请分析本迭代的阻塞项，并同步给工程师和架构师。需要保留执行步骤和产物。", status: "14:38" },
  { id: "initial-decision", kind: "decision", title: "架构师 · Architecture Agent", content: "先拆分约束、依赖和风险，再交给工程师执行 API 校验。上下文会持续写入本会话。", agent: "架构师 / DeepSeek V4 Flash", status: "14:39 · 已完成" },
  { id: "initial-step", kind: "step", title: "工程师 · Engineering Agent", content: "正在验证本地连接器和产物回写路径。", agent: "工程师 / LM Studio", status: "14:40 · 执行中" },
];
let sequence = 0;
const sessionIndex = (() => {
  try { return JSON.parse(localStorage.getItem("hermes.v4.session-index") ?? "[]") as Array<{ id: string; title: string; group: string[] }>; } catch { return []; }
})();
const persistedTitles = Object.fromEntries(sessionIndex.map((session) => [session.id, session.title]));
const persistedGroups = Object.fromEntries(sessionIndex.map((session) => [session.id, session.group]));
const CURSOR_STORAGE_KEY = "hermes.v4.conversation-cursors";
const TIMELINE_STORAGE_KEY = "hermes.v4.conversation-timelines";

function loadConversationCursors(): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(CURSOR_STORAGE_KEY) ?? "{}");
    return parsed && typeof parsed === "object" ? parsed as Record<string, string> : {};
  } catch {
    return {};
  }
}

function saveConversationCursor(sessionId: string, cursor: string): void {
  try {
    const cursors = loadConversationCursors();
    cursors[sessionId] = cursor;
    localStorage.setItem(CURSOR_STORAGE_KEY, JSON.stringify(cursors));
  } catch {
    // SQLite remains the source of truth when browser storage is unavailable.
  }
}

function loadConversationTimeline(sessionId: string): TimelineEntry[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(TIMELINE_STORAGE_KEY) ?? "{}");
    const entries = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>)[sessionId] : undefined;
    return Array.isArray(entries) ? entries as TimelineEntry[] : [];
  } catch {
    return [];
  }
}

function saveConversationTimeline(sessionId: string, entries: TimelineEntry[]): void {
  try {
    const parsed = JSON.parse(localStorage.getItem(TIMELINE_STORAGE_KEY) ?? "{}");
    const timelines = parsed && typeof parsed === "object" ? parsed as Record<string, unknown> : {};
    timelines[sessionId] = entries.slice(-200);
    localStorage.setItem(TIMELINE_STORAGE_KEY, JSON.stringify(timelines));
  } catch {
    // The backend event stream remains the source of truth.
  }
}

function mapEvent(event: ConversationEvent, index: number): TimelineEntry | null {
  const id = event.eventId ?? `${event.sequence ?? index}-${index}`;
  const value = event.value ?? {};
  const type = event.type ?? "";
  if (event.name === "user.message.received") return { id, kind: "user", title: "你", content: String(value.content ?? "") };
  if (event.delta) return { id, kind: "delta", title: "流式输出 · Streaming", content: event.delta, agent: "Agent / API", status: "TEXT_MESSAGE_CONTENT" };
  if (event.name === "turn_finished" || type === "RUN_FINISHED") return { id, kind: "agent", title: "执行完成 · Turn finished", content: String(value.status ?? "completed"), agent: "Agent / API", status: "completed" };
  if (event.name === "turn_failed" || type === "RUN_ERROR") return { id, kind: "agent", title: value.reason === "runtime_cancelled" ? "执行取消 · Turn cancelled" : "执行失败 · Turn failed", content: publicRuntimeError(value.reason), agent: "Agent / API", status: "failed" };
  if (event.name?.includes("tool") || type.startsWith("TOOL_CALL")) return { id, kind: "tool", title: "工具执行 · Tool evidence", content: String(value.public_result ?? value.result ?? event.result ?? event.name ?? event.toolCallName ?? "tool"), agent: "Tool / API", status: event.name?.includes("completed") || type === "TOOL_CALL_END" ? "completed" : "running" };
  if (event.name?.includes("decision")) return { id, kind: "decision", title: "决策摘要 · Decision", content: String(value.summary ?? "decision"), agent: "Supervisor / API" };
  if (event.name === "conversation.status") return { id, kind: "step", title: "会话状态 · Conversation status", content: String(value.status ?? "running"), agent: "Task3 Conversation API" };
  if (event.name === "runtime.status.changed") return { id, kind: "step", title: "Runtime 状态 · Runtime status", content: String(value.status ?? "running"), agent: "Federated Runtime" };
  if (event.name === "turn_queued") return { id, kind: "step", title: "已排队 · Turn queued", content: `command ${String(value.command_id ?? "")}`, agent: "Conversation Worker", status: "queued" };
  return null;
}

export type ConversationStatusPhase = "ready" | "queued" | "running" | "retryable" | "paused" | "completed" | "failed" | "cancelled" | "reconciliation_required";
export type ConversationStatusProjection = { phase: ConversationStatusPhase; label: string; terminal: boolean; commandId?: string };

const readyStatus: ConversationStatusProjection = { phase: "ready", label: "准备就绪 · ready", terminal: false };
const completedStatus: ConversationStatusProjection = { phase: "completed", label: "已完成 · completed", terminal: true };

function statusForEvent(event: ConversationEvent): ConversationStatusProjection | null {
  const commandId = typeof event.value?.command_id === "string" ? event.value.command_id : undefined;
  if (event.name === "orchestration.graph.queued") return { phase: "queued", label: "多 Agent 已排队 · queued", terminal: false, commandId };
  if (event.name === "orchestration.node.progress") return { phase: "running", label: "多 Agent 执行中 · running", terminal: false };
  if (event.name === "orchestration.interrupted") return { phase: "paused", label: "等待人工审批 · needs human", terminal: true };
  if (event.name === "turn_queued") return { phase: "queued", label: "排队中 · queued", terminal: false, commandId };
  if (event.name === "conversation.status") {
    const status = String(event.value?.status ?? "running");
    return status === "paused"
      ? { phase: "paused", label: "已暂停 · paused", terminal: true }
      : { phase: "running", label: "执行中 · running", terminal: false };
  }
  if (event.name === "runtime.status.changed") {
    const status = String(event.value?.status ?? "running");
    if (status === "completed") return { ...completedStatus, commandId };
    if (status === "cancelled") return { phase: "cancelled", label: "已取消 · runtime_cancelled", terminal: true, commandId };
    if (status === "failed") return { phase: "failed", label: "执行失败 · runtime_failed", terminal: true, commandId };
    if (status === "paused") return { phase: "paused", label: "已暂停 · paused", terminal: true, commandId };
    if (status === "queued") return { phase: "queued", label: "排队中 · queued", terminal: false, commandId };
    return { phase: "running", label: "执行中 · running", terminal: false, commandId };
  }
  if (event.name === "turn_retryable") return { phase: "retryable", label: "等待重试 · retryable", terminal: false, commandId };
  if (event.name === "turn_finished" || event.type === "RUN_FINISHED") return { ...completedStatus, commandId };
  if (event.name === "turn_failed" || event.type === "RUN_ERROR") {
    if (event.value?.reason === "runtime_cancelled") return { phase: "cancelled", label: "已取消 · runtime_cancelled", terminal: true, commandId };
    return event.value?.response_status === "reconciliation_required"
      ? { phase: "reconciliation_required", label: "需要对账 · reconciliation required", terminal: true, commandId }
      : { phase: "failed", label: `执行失败 · ${publicRuntimeError(event.value?.reason)}`, terminal: true, commandId };
  }
  if (event.delta) return { phase: "running", label: "执行中 · running", terminal: false };
  return null;
}

export function reduceConversationStatus(
  current: ConversationStatusProjection | undefined,
  event: ConversationEvent,
): ConversationStatusProjection {
  const prior = current ?? readyStatus;
  const next = statusForEvent(event);
  if (!next) return prior;
  const sticky = prior.terminal;
  const regressive = next.phase === "queued" || next.phase === "running" || next.phase === "retryable";
  const explicitResume = event.name === "conversation.status"
    && event.value?.status === "running"
    && event.value?.command_id === undefined;
  const newCommand = prior.phase !== "paused"
    && next.phase === "queued"
    && prior.commandId !== undefined
    && next.commandId !== undefined
    && prior.commandId !== next.commandId;
  return sticky && regressive && !explicitResume && !newCommand ? prior : { ...next, commandId: next.commandId ?? prior.commandId };
}

export function describeConversationError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const normalized = raw.toLowerCase();
  if (normalized.includes("no enabled model provider")) return "未配置启用的模型供应商，请先在“模型供应商”中解锁保险库并启用 Provider";
  if (normalized.includes("vault") || normalized.includes("locked") || normalized.includes("保险库")) return "模型保险库已锁定，请先在“模型供应商”中解锁";
  if (normalized.includes("timeout") || normalized.includes("readtimeout") || normalized.includes("timed out") || normalized.includes("aborted")) return "模型请求超时，请检查 LM Studio 或云端 API 是否可用";
  return raw || "未知的本地服务错误";
}

function publicRuntimeError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const categories = ["runtime_unavailable", "runtime_admission_blocked", "runtime_selection_conflict", "provider_unavailable", "provider_incompatible", "provider_grant_failed", "runtime_failed", "runtime_cancelled", "reconciliation_required"];
  return categories.find((category) => raw.split(/[^a-z_]+/).includes(category)) ?? "runtime_unavailable";
}

// Match the broker's public protocol restrictions, never infer them from an ID.
function compatibleProvider(runtime: RuntimeSelector, provider: ProviderProfile | undefined, model: string): boolean {
  if (!provider?.enabled) return false;
  if (!Object.keys(provider.model_aliases).includes(model) && !Object.values(provider.model_aliases).includes(model)) return false;
  if (runtime === "dsh") return provider.protocol === "deepseek" && Object.keys(provider.headers ?? {}).length === 0;
  if (runtime === "goose") return ["deepseek", "lmstudio", "openai", "openai_chat", "openai_compatible"].includes(provider.protocol);
  return true;
}

type FrozenSelection = { runtime: "" | RuntimeSelector; providerId: string; model: string; pending: boolean; commandId?: string };

export function ConversationWorkspace() {
  const [sessionId, setSessionId] = useState("ui-session-0");
  const [titles, setTitles] = useState({ ...seededTitles, ...persistedTitles });
  const [groups, setGroups] = useState({ ...seededGroups, ...persistedGroups });
  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [source, setSource] = useState("Task 3 REST/SSE");
  const [statusProjection, setStatusProjection] = useState<ConversationStatusProjection>(completedStatus);
  const status = statusProjection.label;
  const [pending, setPending] = useState(false);
  const [paused, setPaused] = useState(false);
  const [artifactId, setArtifactId] = useState("markdown");
  const [canvasOpen, setCanvasOpen] = useState(true);
  const [modelProfiles, setModelProfiles] = useState<AgentModelProfile[]>(loadAgentModelProfiles);
  const [sequential, setSequential] = useState(emptySequentialState);
  const [research, setResearch] = useState(emptyResearchGraphState);
  const [selectedModel, setSelectedModel] = useState("local-agent");
  const [selectedProviderId, setSelectedProviderId] = useState("lmstudio");
  const [selectedRuntime, setSelectedRuntime] = useState<"" | RuntimeSelector>("");
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [runtimeDiagnostic, setRuntimeDiagnostic] = useState<EngineHostV2Diagnostic | null>(null);
  const cursorsRef = useRef<Record<string, string>>(loadConversationCursors());
  const seenEventsRef = useRef<Record<string, Set<string>>>({});
  const watcherGenerationRef = useRef(0);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;
  const selectionsRef = useRef<Record<string, FrozenSelection>>({});
  const group = groups[sessionId] ?? ["产品经理"];
  const title = titles[sessionId] ?? "新建会话 · New task";
  const activeProfile = useMemo(() => agentModelProfileFor(modelProfiles, group[0] ?? "产品经理"), [group, modelProfiles]);
  const allModelOptions = useMemo(() => {
    const configured = providers.filter(provider => provider.enabled).flatMap(provider => [...new Set(Object.values(provider.model_aliases))].filter(Boolean).map(model => ({ id: `provider:${provider.id}:${model}`, providerId: provider.id, model, label: `${provider.name} · ${model}` })));
    const agents = modelProfiles.filter(profile => profile.enabled).map(profile => ({ id: profile.id, providerId: profile.providerId, model: profile.model, label: `${profile.name} · ${providers.find(p => p.id === profile.providerId)?.name ?? providerLabels[profile.providerId] ?? profile.providerId} · ${profile.model}` }));
    const unique = new Map<string, typeof agents[number]>();
    for (const option of [...agents, ...configured]) if (!unique.has(`${option.providerId}/${option.model}`)) unique.set(`${option.providerId}/${option.model}`, option);
    return [...unique.values()];
  }, [modelProfiles, providers]);
  const modelOptions = useMemo(() => allModelOptions.filter(option => !selectedRuntime || compatibleProvider(selectedRuntime, providers.find(p => p.id === option.providerId), option.model) || (pending && option.providerId === selectedProviderId && option.model === selectedModel)), [allModelOptions, providers, selectedRuntime, pending, selectedProviderId, selectedModel]);
  const runtimeOptions = useMemo<RuntimeOption[]>(() => (["python-term", "goose", "dsh"] as const).map(selector => {
    const diagnostic = runtimeDiagnostic?.v2.runtimes.find(runtime => runtime.selector === selector);
    const compatible = allModelOptions.some(option => compatibleProvider(selector, providers.find(p => p.id === option.providerId), option.model));
    return { selector, label: runtimeLabels[selector], selectable: diagnostic?.selectable_for_new_commands === true && compatible,
      trustStatus: diagnostic?.trust_status ?? null, reason: diagnostic?.admission_reason ?? (!diagnostic?.selectable_for_new_commands ? "runtime_unavailable" : !compatible ? "provider_incompatible" : null) };
  }), [runtimeDiagnostic, providers, allModelOptions]);
  const selectedProviderLabel = providerLabels[selectedProviderId as keyof typeof providerLabels] ?? selectedProviderId;
  const artifact = useMemo(() => artifacts.find((candidate) => candidate.id === artifactId) ?? artifacts[0], [artifactId]);
  const Renderer = registry.resolve(artifact.kind);

  useEffect(() => {
    let active = true;
    const generation = ++watcherGenerationRef.current;
    // Replay starts a new cursor lineage. An empty server history must not
    // leave a stale browser cursor active for the next poll/new command.
    cursorsRef.current[sessionId] = "";
    const seen = new Set<string>();
    seenEventsRef.current[sessionId] = seen;
    let hydratedGraph = false;
    setEntries([]);
    setPending(selectionsRef.current[sessionId]?.pending ?? false);
    setSequential(emptySequentialState());
    setResearch(emptyResearchGraphState());
    setStatusProjection(readyStatus);
    setSource("Task 3 REST/SSE");
    const consume = async () => {
      if (!active || watcherGenerationRef.current !== generation) return;
      try {
        await conversationApi.createSession(sessionId);
        // A cursor is not a snapshot: always rebuild projections from durable
        // events when entering a session, including when browser caches vanished.
        const resumeCursor = hydratedGraph ? cursorsRef.current[sessionId] : undefined;
        const events = await conversationApi.events(sessionId, resumeCursor);
        const graphEvents = events;
        if (!active || watcherGenerationRef.current !== generation) return;
        const fresh = events.filter((event) => {
          const id = event.cursor ?? event.eventId ?? `${event.sequence ?? ""}:${event.name ?? event.type ?? ""}`;
          if (seen.has(id)) return false;
          seen.add(id);
          return true;
        });
        const mapped = fresh.map(mapEvent).filter((entry): entry is TimelineEntry => Boolean(entry));
        if (!hydratedGraph) {
          setSequential(graphEvents.reduce(reduceSequentialEvent, emptySequentialState()));
          setResearch(graphEvents.reduce(reduceResearchEvent, emptyResearchGraphState()));
          const recovered = graphEvents.reduce(reduceConversationStatus, readyStatus);
          setStatusProjection(recovered);
          if (["queued", "running", "retryable", "paused"].includes(recovered.phase)) setPending(true);
          setEntries(graphEvents.map(mapEvent).filter((entry): entry is TimelineEntry => Boolean(entry)));
          hydratedGraph = true;
        } else if (fresh.length) {
          setSequential((current) => fresh.reduce(reduceSequentialEvent, current));
          setResearch((current) => fresh.reduce(reduceResearchEvent, current));
          if (mapped.length) setEntries((current) => {
              const next = current.concat(mapped);
              saveConversationTimeline(sessionId, next);
              return next;
            });
        }
        for (const event of fresh) {
          const queuedCommand = event.name === "turn_queued" ? event.value?.command_id : undefined;
          if (typeof queuedCommand === "string") {
            void engineHostApi.runtimeAdmission(sessionId, queuedCommand).then(admission => {
              if (!active || watcherGenerationRef.current !== generation || admission.state !== "ready" || !admission.selector || !isRuntimeSelector(admission.selector)) return;
              const label = runtimeLabels[admission.selector];
              setEntries(current => current.some(entry => entry.id === `admission-${queuedCommand}`) ? current : [...current, { id: `admission-${queuedCommand}`, kind: "step", title: "准入已确认 · Admission ready", content: "Grant 阶段暂无独立状态播报", agent: label }]);
            }).catch(() => { /* Legacy commands may have no runtime admission. */ });
          }
          if (event.cursor) {
            cursorsRef.current[sessionId] = event.cursor;
            saveConversationCursor(sessionId, event.cursor);
          }
          setStatusProjection((current) => {
            const next = reduceConversationStatus(current, event);
            if (next.terminal && next.phase !== "paused") {
              const frozen = selectionsRef.current[sessionId];
              if (!frozen?.pending || !frozen.commandId || next.commandId === frozen.commandId) {
                if (frozen) frozen.pending = false;
                setPending(false);
              }
            } else if (["queued", "running", "retryable"].includes(next.phase)) setPending(true);
            return next;
          });
          if (event.name === "turn_failed" || event.type === "RUN_ERROR" || (event.name === "runtime.status.changed" && ["failed", "cancelled"].includes(String(event.value?.status)))) {
            void engineHostApi.v2Status().then(setRuntimeDiagnostic).catch(() => setRuntimeDiagnostic(null));
          }
        }
        setSource("Task 3 REST/SSE · cursor");
      } catch (error: unknown) {
        if (active) {
          setSource("Task 3 API · 等待本地服务");
          if (error instanceof Error && error.message.includes("session not found")) setStatusProjection({ phase: "ready", label: "会话尚未同步 · waiting", terminal: false });
        }
      }
      if (active && watcherGenerationRef.current === generation) window.setTimeout(() => void consume(), 350);
    };
    void consume();
    return () => { active = false; };
  }, [sessionId]);

  useEffect(() => {
    let active = true;
    const refreshRuntimeStatus = async () => {
      try {
        const diagnostic = await engineHostApi.v2Status();
        if (active) setRuntimeDiagnostic(diagnostic);
      } catch {
        if (active) setRuntimeDiagnostic(null);
      }
    };
    void refreshRuntimeStatus();
    void providerApi.listProviders().then(value => { if (active) setProviders(value); }).catch(() => { if (active) setProviders([]); });
    const timer = window.setInterval(() => void refreshRuntimeStatus(), 5_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    let active = true;
    void agentApi.list().then((records) => {
      if (active) setModelProfiles(mergeAgentRecords(records));
    }).catch(() => { /* Agent settings displays the actionable backend error. */ });
    return () => { active = false; };
  }, [sessionId]);

  useEffect(() => {
    const frozen = selectionsRef.current[sessionId];
    if (frozen?.pending) {
      setSelectedRuntime(frozen.runtime);
      setSelectedModel(frozen.model);
      setSelectedProviderId(frozen.providerId);
      return;
    }
    setSelectedRuntime("");
    setSelectedModel(activeProfile.model);
    setSelectedProviderId(activeProfile.providerId);
  }, [activeProfile, sessionId]);

  const changeRuntime = (runtime: "" | RuntimeSelector) => {
    if (pending) return;
    setSelectedRuntime(runtime);
    if (!runtime) return;
    const compatible = allModelOptions.filter(option => compatibleProvider(runtime, providers.find(p => p.id === option.providerId), option.model));
    if (!compatible.some(option => option.providerId === selectedProviderId && option.model === selectedModel) && compatible[0]) {
      setSelectedProviderId(compatible[0].providerId);
      setSelectedModel(compatible[0].model);
    }
  };

  const createGroup = (roles: string[], mode: "single" | "multi") => {
    const createdSessionId = `ui-group-${Date.now()}-${++sequence}`;
    const newTitle = mode === "single" ? `${roles[0]} · 新任务` : `${roles.slice(0, 3).join("、")}${roles.length > 3 ? " 等" : ""} · 协同会话`;
    setGroups((current) => ({ ...current, [createdSessionId]: roles }));
    setTitles((current) => ({ ...current, [createdSessionId]: newTitle }));
    setEntries([]);
    setSessionId(createdSessionId);
    setStatusProjection({ phase: "ready", label: "准备执行 · ready", terminal: false });
    return { sessionId: createdSessionId, title: newTitle };
  };
  const selectSession = (nextSessionId: string) => { setSessionId(nextSessionId); setPaused(false); };
  const addUserEntry = (prompt: string, kind: "user" | "decision" = "user") => setEntries((current) => {
    const next = [...current, { id: `local-${++sequence}`, kind, title: kind === "user" ? "你" : "人工介入 · Human", content: prompt, status: "刚刚" }];
    saveConversationTimeline(sessionId, next);
    return next;
  });
  const send = (prompt: string, contexts: string[] = []) => {
    if (pending || selectionsRef.current[sessionId]?.pending) return;
    const chosenRuntime = selectedRuntime
      ? runtimeOptions.find((option) => option.selector === selectedRuntime)
      : undefined;
    if (selectedRuntime && (chosenRuntime?.selectable !== true || !compatibleProvider(selectedRuntime, providers.find(p => p.id === selectedProviderId), selectedModel))) {
      const reason = chosenRuntime?.reason ?? "runtime_unavailable";
      setStatusProjection({ phase: "failed", label: `执行失败 · ${reason}`, terminal: true });
      setEntries((current) => [...current, { id: `error-${++sequence}`, kind: "tool", title: "请求失败 · Runtime admission", content: reason, agent: "Runtime Admission API", status: "failed" }]);
      return;
    }
    const apiPrompt = contexts.length ? `${prompt}\n\n[本轮上下文]\n${contexts.map((context) => `- ${context}`).join("\n")}` : prompt;
    addUserEntry(prompt);
    let agentBindings: Array<{ agent_id: string; expected_version: number }> = [];
    try {
      agentBindings = orderedMentionBindings(prompt, modelProfiles);
    } catch (error) {
      const reason = describeConversationError(error);
      setStatusProjection({ phase: "failed", label: `执行失败 · ${reason}`, terminal: true });
      setEntries((current) => [...current, { id: `error-${++sequence}`, kind: "tool", title: "请求失败 · Agent 配置", content: reason, agent: "Agent Profile API", status: "failed" }]);
      return;
    }
    setPending(true);
    const frozen: FrozenSelection = { runtime: selectedRuntime, providerId: selectedProviderId, model: selectedModel, pending: true };
    selectionsRef.current[sessionId] = frozen;
    setStatusProjection({ phase: "queued", label: "排队中 · queued", terminal: false });
    const commandId = createConversationCommandId("message", sessionId);
    void conversationApi.sendMessage(sessionId, apiPrompt, commandId, selectedModel, selectedProviderId, agentBindings, selectedRuntime || undefined).then((result) => {
      frozen.commandId = result.command_id;
      if (result.status === "paused") frozen.pending = false;
      if (activeSessionRef.current !== sessionId) return;
      setSource("Task 3 REST/SSE · queued");
      // Only consumed SSE events advance the read cursor. The POST cursor is
      // an acknowledgement, not evidence that queued/status events were read.
      if (result.status === "paused") {
        setStatusProjection({ phase: "paused", label: "已暂停 · paused", terminal: true });
        setPending(false);
        frozen.pending = false;
      }
      if (selectedRuntime) {
        void engineHostApi.runtimeAdmission(sessionId, result.command_id).then((admission) => {
          if (activeSessionRef.current !== sessionId) return;
          if (admission.state === "blocked") {
            setStatusProjection({ phase: "failed", label: "执行失败 · runtime_admission_blocked", terminal: true });
            setPending(false);
            frozen.pending = false;
            void engineHostApi.v2Status().then(setRuntimeDiagnostic).catch(() => setRuntimeDiagnostic(null));
          } else if (admission.state === "ready") {
            setEntries(current => current.some(entry => entry.id === `admission-${result.command_id}`) ? current : [...current, { id: `admission-${result.command_id}`, kind: "step", title: "准入已确认 · Admission ready", content: "Grant 阶段暂无独立状态播报", agent: runtimeLabels[selectedRuntime] }]);
          }
        }).catch(() => { /* SSE remains authoritative when the read-only diagnostic is temporarily unavailable. */ });
      }
    }).catch(async (error: unknown) => {
      const reason = selectedRuntime ? publicRuntimeError(error) : describeConversationError(error);
      frozen.pending = false;
      {
        try {
          const diagnostic = await engineHostApi.v2Status();
          setRuntimeDiagnostic(diagnostic);
        } catch {
          // Preserve the original stable HTTP category when diagnostics are unavailable.
        }
      }
      if (activeSessionRef.current !== sessionId) return;
      setSource("Task 3 API error");
      setStatusProjection({ phase: "failed", label: `执行失败 · ${reason}`, terminal: true });
      setEntries((current) => [...current, { id: `error-${++sequence}`, kind: "tool", title: "请求失败 · API diagnosis", content: `conversation.messages → ${reason}`, agent: "Task3 Conversation API", status: "failed" }]);
      setPending(false);
    });
  };
  const intervene = (prompt: string) => {
    addUserEntry(prompt, "decision");
    setStatusProjection({ phase: "queued", label: "介入已排队 · queued", terminal: false });
    void conversationApi.intervene(sessionId, prompt, createConversationCommandId("intervention", sessionId)).then(() => setSource("Task 3 intervention API")).catch((error: unknown) => { setSource("Task 3 API error"); setStatusProjection({ phase: "failed", label: `介入失败 · ${describeConversationError(error)}`, terminal: true }); });
  };
  const togglePause = () => {
    const commandId = createConversationCommandId(paused ? "resume" : "pause", sessionId);
    setPaused((current) => !current);
    void (paused ? conversationApi.resume(sessionId, commandId) : conversationApi.pause(sessionId, commandId)).then(() => setStatusProjection(paused ? { phase: "running", label: "已恢复 · running", terminal: false } : { phase: "paused", label: "已暂停 · paused", terminal: true })).catch(() => setStatusProjection({ phase: "failed", label: "状态同步失败 · fixture", terminal: true }));
  };
  const approveOrchestration = () => {
    if (!sequential.commandId) return;
    setStatusProjection({ phase: "queued", label: "人工审批已提交 · queued", terminal: false });
    void conversationApi.resumeOrchestration(sessionId, sequential.commandId, createConversationCommandId("orchestration-resume", sessionId)).then(() => {
      setSequential((current) => ({ ...current, interrupted: undefined }));
    }).catch((error: unknown) => setStatusProjection({ phase: "failed", label: `审批失败 · ${describeConversationError(error)}`, terminal: true }));
  };
  const htmlArtifact = Object.values(sequential.artifacts).filter((item) => item.mediaType === "text/html").at(-1);
  const resumeResearch = (preference?: string) => {
    if (!research.graphRunId || !research.interruptId) return;
    setStatusProjection({ phase: "queued", label: "人工审核已批准 · queued", terminal: false });
    void graphPlanApi.resumeInterrupt(research.graphRunId, research.interruptId, createConversationCommandId("research-interrupt", sessionId), preference).catch((error: unknown) => setStatusProjection({ phase: "failed", label: `审核恢复失败 · ${describeConversationError(error)}`, terminal: true }));
  };

  return <section className="conversation-workspace" aria-label="会话工作区">
    <SessionSidebar group={group} activeSessionId={sessionId} onCreateGroup={createGroup} onSessionChange={selectSession} agentProfiles={modelProfiles} />
    <main className="conversation-main">
      <header className="conversation-header"><div className="conversation-heading"><div className="agent-avatar agent-avatar-blue">{group[0]?.slice(0, 1) ?? "苏"}</div><div><h2 data-testid="conversation-title">{title}</h2><p>项目实施讨论 · 持续上下文 · {group.length > 1 ? `${group.length} Agents` : "单 Agent"}</p></div></div><div className="conversation-header-actions"><div className="agent-stack" data-testid="agent-avatar-stack" aria-label="当前会话 Agent">{group.slice(0, 3).map((agent) => <span key={agent}>{agent.slice(0, 1)}</span>)}{group.length > 3 && <span>+{group.length - 3}</span>}</div><button type="button" className="quiet" onClick={togglePause}>{paused ? "恢复" : "暂停"}</button><button type="button" className="quiet" aria-label="会话详情">⋯</button></div></header>
      <div className="conversation-source" data-testid="conversation-source">来源 · {source} · session {sessionId}</div>
      <div className={`conversation-status ${status.includes("执行中") ? "running" : ""}`} data-testid="conversation-status" aria-live="polite">{status}</div>
      <div className="conversation-execution-panels">
        <SequentialGraph state={sequential} profiles={modelProfiles} onApprove={approveOrchestration} />
        <PlanApproval sessionId={sessionId} onApproved={() => setStatusProjection({ phase: "queued", label: "研究计划已批准并排队 · queued", terminal: false })} />
        <GraphRun state={research} onResume={resumeResearch} sessionId={sessionId} />
      </div>
      <Timeline entries={entries} group={group} provider={selectedProviderLabel} model={selectedModel} status={status} />
      <Composer onSend={send} onIntervene={intervene} pending={pending} paused={paused} model={selectedModel} providerId={selectedProviderId} modelOptions={modelOptions} onModelChange={(providerId, model) => { if (!pending) { setSelectedProviderId(providerId); setSelectedModel(model); } }} runtime={selectedRuntime} runtimeOptions={runtimeOptions} onRuntimeChange={changeRuntime} />
    </main>
    {canvasOpen ? <aside className="artifacts-canvas" aria-label="智能画布 · Artifacts"><header><strong>智能画布 · Artifacts</strong><button type="button" className="quiet" aria-label="折叠画布" onClick={() => setCanvasOpen(false)}>折叠</button></header>{htmlArtifact ? <HtmlArtifactPreview artifactId={htmlArtifact.artifactId} /> : <><nav aria-label="Artifacts 列表">{artifacts.map((item) => <button key={item.id} type="button" className="quiet" aria-pressed={artifactId === item.id} onClick={() => setArtifactId(item.id)}>{item.title}</button>)}</nav><section className="artifact-preview"><small>version v3 · {artifact.mimeType}</small><h3>{artifact.title}</h3><Renderer artifact={artifact} /></section></>}<section className="artifact-version-card"><strong>版本卡片 · Version cards</strong><p>当前结果 · 历史 Attempt · 审核证据</p></section></aside> : <button type="button" className="quiet canvas-reopen" aria-label="打开画布" onClick={() => setCanvasOpen(true)}>打开画布</button>}
  </section>;
}

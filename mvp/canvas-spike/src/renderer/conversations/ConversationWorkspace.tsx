import { useEffect, useMemo, useRef, useState } from "react";
import { artifacts } from "../artifacts";
import { agentApi, conversationApi, type ConversationEvent } from "../api";
import { registry } from "../renderers";
import { Composer } from "./Composer";
import { SessionSidebar } from "./SessionSidebar";
import { Timeline, type TimelineEntry } from "./Timeline";
import { createConversationCommandId } from "./commandIds";
import { agentModelProfileFor, loadAgentModelProfiles, mergeAgentRecords, orderedMentionBindings, providerLabels, type AgentModelProfile } from "../models/agentConfig";
import { HtmlArtifactPreview } from "./HtmlArtifactPreview";
import { SequentialGraph } from "./SequentialGraph";
import { emptySequentialState, reduceSequentialEvent } from "./sequentialReducer";

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
  if (event.delta) return { id, kind: "delta", title: "流式输出 · Streaming", content: event.delta, agent: "Agent / API", status: "TEXT_MESSAGE_CONTENT" };
  if (event.name === "turn_finished" || type === "RUN_FINISHED") return { id, kind: "agent", title: "执行完成 · Turn finished", content: String(value.status ?? "completed"), agent: "Agent / API", status: "completed" };
  if (event.name === "turn_failed" || type === "RUN_ERROR") return { id, kind: "agent", title: "执行失败 · Turn failed", content: String(value.reason ?? "agent_error"), agent: "Agent / API", status: "failed" };
  if (event.name?.includes("tool") || type.startsWith("TOOL_CALL")) return { id, kind: "tool", title: "工具执行 · Tool evidence", content: String(value.public_result ?? value.result ?? event.name ?? event.toolCallName ?? "tool"), agent: "Tool / API", status: event.name?.includes("completed") || type === "TOOL_CALL_END" ? "completed" : "running" };
  if (event.name?.includes("decision")) return { id, kind: "decision", title: "决策摘要 · Decision", content: String(value.summary ?? "decision"), agent: "Supervisor / API" };
  if (event.name === "conversation.status") return { id, kind: "step", title: "会话状态 · Conversation status", content: String(value.status ?? "running"), agent: "Task3 Conversation API" };
  if (event.name === "turn_queued") return { id, kind: "step", title: "已排队 · Turn queued", content: `command ${String(value.command_id ?? "")}`, agent: "Conversation Worker", status: "queued" };
  return null;
}

function statusForEvent(event: ConversationEvent): { label: string; terminal: boolean } | null {
  if (event.name === "orchestration.graph.queued") return { label: "多 Agent 已排队 · queued", terminal: false };
  if (event.name === "orchestration.node.progress") return { label: "多 Agent 执行中 · running", terminal: false };
  if (event.name === "orchestration.interrupted") return { label: "等待人工审批 · needs human", terminal: true };
  if (event.name === "turn_queued") return { label: "排队中 · queued", terminal: false };
  if (event.name === "conversation.status") {
    const status = String(event.value?.status ?? "running");
    return status === "paused"
      ? { label: "已暂停 · paused", terminal: true }
      : { label: "执行中 · running", terminal: false };
  }
  if (event.name === "turn_retryable") return { label: "等待重试 · retryable", terminal: false };
  if (event.name === "turn_finished" || event.type === "RUN_FINISHED") return { label: "已完成 · completed", terminal: true };
  if (event.name === "turn_failed" || event.type === "RUN_ERROR") return { label: "执行失败 · failed", terminal: true };
  if (event.delta) return { label: "执行中 · running", terminal: false };
  return null;
}

export function describeConversationError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const normalized = raw.toLowerCase();
  if (normalized.includes("no enabled model provider")) return "未配置启用的模型供应商，请先在“模型供应商”中解锁保险库并启用 Provider";
  if (normalized.includes("vault") || normalized.includes("locked") || normalized.includes("保险库")) return "模型保险库已锁定，请先在“模型供应商”中解锁";
  if (normalized.includes("timeout") || normalized.includes("readtimeout") || normalized.includes("timed out") || normalized.includes("aborted")) return "模型请求超时，请检查 LM Studio 或云端 API 是否可用";
  return raw || "未知的本地服务错误";
}

export function ConversationWorkspace() {
  const [sessionId, setSessionId] = useState("ui-session-0");
  const [titles, setTitles] = useState({ ...seededTitles, ...persistedTitles });
  const [groups, setGroups] = useState({ ...seededGroups, ...persistedGroups });
  const [entries, setEntries] = useState<TimelineEntry[]>(initialTimeline);
  const [source, setSource] = useState("Task 3 REST/SSE");
  const [status, setStatus] = useState("已完成 · completed");
  const [pending, setPending] = useState(false);
  const [paused, setPaused] = useState(false);
  const [artifactId, setArtifactId] = useState("markdown");
  const [canvasOpen, setCanvasOpen] = useState(true);
  const [modelProfiles, setModelProfiles] = useState<AgentModelProfile[]>(loadAgentModelProfiles);
  const [sequential, setSequential] = useState(emptySequentialState);
  const [selectedModel, setSelectedModel] = useState("local-agent");
  const [selectedProviderId, setSelectedProviderId] = useState("lmstudio");
  const cursorsRef = useRef<Record<string, string>>(loadConversationCursors());
  const seenEventsRef = useRef<Record<string, Set<string>>>({});
  const watcherGenerationRef = useRef(0);
  const group = groups[sessionId] ?? ["产品经理"];
  const title = titles[sessionId] ?? "新建会话 · New task";
  const activeProfile = useMemo(() => agentModelProfileFor(modelProfiles, group[0] ?? "产品经理"), [group, modelProfiles]);
  const modelOptions = useMemo(() => modelProfiles.filter((profile) => profile.enabled).map((profile) => ({ id: profile.id, providerId: profile.providerId, model: profile.model, label: `${profile.name} · ${providerLabels[profile.providerId]} · ${profile.model}` })), [modelProfiles]);
  const selectedProviderLabel = providerLabels[selectedProviderId as keyof typeof providerLabels] ?? selectedProviderId;
  const artifact = useMemo(() => artifacts.find((candidate) => candidate.id === artifactId) ?? artifacts[0], [artifactId]);
  const Renderer = registry.resolve(artifact.kind);

  useEffect(() => {
    let active = true;
    const generation = ++watcherGenerationRef.current;
    const seen = seenEventsRef.current[sessionId] ?? new Set<string>();
    seenEventsRef.current[sessionId] = seen;
    const storedTimeline = loadConversationTimeline(sessionId);
    setEntries(storedTimeline.length ? storedTimeline : (sessionId === "ui-session-0" ? initialTimeline : []));
    setSequential(emptySequentialState());
    setStatus("准备就绪 · ready");
    setSource("Task 3 REST/SSE");
    const consume = async () => {
      if (!active || watcherGenerationRef.current !== generation) return;
      try {
        await conversationApi.createSession(sessionId);
        const events = await conversationApi.events(sessionId, storedTimeline.length ? cursorsRef.current[sessionId] : undefined);
        if (!active || watcherGenerationRef.current !== generation) return;
        const fresh = events.filter((event) => {
          const id = event.cursor ?? event.eventId ?? `${event.sequence ?? ""}:${event.name ?? event.type ?? ""}`;
          if (seen.has(id)) return false;
          seen.add(id);
          return true;
        });
        const mapped = fresh.map(mapEvent).filter((entry): entry is TimelineEntry => Boolean(entry));
        if (fresh.length) setSequential((current) => fresh.reduce(reduceSequentialEvent, current));
        if (mapped.length) {
          setEntries((current) => {
            const next = current.concat(mapped);
            saveConversationTimeline(sessionId, next);
            return next;
          });
        }
        for (const event of fresh) {
          if (event.cursor) {
            cursorsRef.current[sessionId] = event.cursor;
            saveConversationCursor(sessionId, event.cursor);
          }
          const nextStatus = statusForEvent(event);
          if (nextStatus) {
            setStatus(nextStatus.label);
            if (nextStatus.terminal) setPending(false);
          }
        }
        setSource("Task 3 REST/SSE · cursor");
      } catch (error: unknown) {
        if (active) {
          setSource("Task 3 API · 等待本地服务");
          if (error instanceof Error && error.message.includes("session not found")) setStatus("会话尚未同步 · waiting");
        }
      }
      if (active && watcherGenerationRef.current === generation) window.setTimeout(() => void consume(), 350);
    };
    void consume();
    return () => { active = false; };
  }, [sessionId]);

  useEffect(() => {
    let active = true;
    void agentApi.list().then((records) => {
      if (active) setModelProfiles(mergeAgentRecords(records));
    }).catch(() => { /* Agent settings displays the actionable backend error. */ });
    return () => { active = false; };
  }, [sessionId]);

  useEffect(() => {
    setSelectedModel(activeProfile.model);
    setSelectedProviderId(activeProfile.providerId);
  }, [activeProfile]);

  const createGroup = (roles: string[], mode: "single" | "multi") => {
    const createdSessionId = `ui-group-${Date.now()}-${++sequence}`;
    const newTitle = mode === "single" ? `${roles[0]} · 新任务` : `${roles.slice(0, 3).join("、")}${roles.length > 3 ? " 等" : ""} · 协同会话`;
    setGroups((current) => ({ ...current, [createdSessionId]: roles }));
    setTitles((current) => ({ ...current, [createdSessionId]: newTitle }));
    setEntries([]);
    setSessionId(createdSessionId);
    setStatus("准备执行 · ready");
    return { sessionId: createdSessionId, title: newTitle };
  };
  const selectSession = (nextSessionId: string) => { setSessionId(nextSessionId); setPaused(false); };
  const addUserEntry = (prompt: string, kind: "user" | "decision" = "user") => setEntries((current) => {
    const next = [...current, { id: `local-${++sequence}`, kind, title: kind === "user" ? "你" : "人工介入 · Human", content: prompt, status: "刚刚" }];
    saveConversationTimeline(sessionId, next);
    return next;
  });
  const send = (prompt: string, contexts: string[] = []) => {
    const apiPrompt = contexts.length ? `${prompt}\n\n[本轮上下文]\n${contexts.map((context) => `- ${context}`).join("\n")}` : prompt;
    addUserEntry(prompt);
    let agentBindings: Array<{ agent_id: string; expected_version: number }> = [];
    try {
      agentBindings = orderedMentionBindings(prompt, modelProfiles);
    } catch (error) {
      const reason = describeConversationError(error);
      setStatus(`执行失败 · ${reason}`);
      setEntries((current) => [...current, { id: `error-${++sequence}`, kind: "tool", title: "请求失败 · Agent 配置", content: reason, agent: "Agent Profile API", status: "failed" }]);
      return;
    }
    setPending(true);
    setStatus("排队中 · queued");
    const commandId = createConversationCommandId("message", sessionId);
    void conversationApi.sendMessage(sessionId, apiPrompt, commandId, selectedModel, selectedProviderId, agentBindings).then((result) => {
      setSource("Task 3 REST/SSE · queued");
      if (result.cursor) {
        cursorsRef.current[sessionId] = result.cursor;
        saveConversationCursor(sessionId, result.cursor);
      }
      if (result.status === "paused") {
        setStatus("已暂停 · paused");
        setPending(false);
      }
    }).catch((error: unknown) => {
      const reason = describeConversationError(error);
      setSource("Task 3 API error");
      setStatus(`执行失败 · ${reason}`);
      setEntries((current) => [...current, { id: `error-${++sequence}`, kind: "tool", title: "请求失败 · API diagnosis", content: `conversation.messages → ${reason}`, agent: "Task3 Conversation API", status: "failed" }]);
      setPending(false);
    });
  };
  const intervene = (prompt: string) => {
    addUserEntry(prompt, "decision");
    setStatus("介入已排队 · queued");
    void conversationApi.intervene(sessionId, prompt, createConversationCommandId("intervention", sessionId)).then(() => setSource("Task 3 intervention API")).catch((error: unknown) => { setSource("Task 3 API error"); setStatus(`介入失败 · ${describeConversationError(error)}`); });
  };
  const togglePause = () => {
    const commandId = createConversationCommandId(paused ? "resume" : "pause", sessionId);
    setPaused((current) => !current);
    void (paused ? conversationApi.resume(sessionId, commandId) : conversationApi.pause(sessionId, commandId)).then(() => setStatus(paused ? "已恢复 · running" : "已暂停 · paused")).catch(() => setStatus("状态同步失败 · fixture"));
  };
  const approveOrchestration = () => {
    if (!sequential.commandId) return;
    setStatus("人工审批已提交 · queued");
    void conversationApi.resumeOrchestration(sessionId, sequential.commandId, createConversationCommandId("orchestration-resume", sessionId)).then(() => {
      setSequential((current) => ({ ...current, interrupted: undefined }));
    }).catch((error: unknown) => setStatus(`审批失败 · ${describeConversationError(error)}`));
  };
  const htmlArtifact = Object.values(sequential.artifacts).filter((item) => item.mediaType === "text/html").at(-1);

  return <section className="conversation-workspace" aria-label="会话工作区">
    <SessionSidebar group={group} activeSessionId={sessionId} onCreateGroup={createGroup} onSessionChange={selectSession} agentProfiles={modelProfiles} />
    <main className="conversation-main">
      <header className="conversation-header"><div className="conversation-heading"><div className="agent-avatar agent-avatar-blue">{group[0]?.slice(0, 1) ?? "苏"}</div><div><h2 data-testid="conversation-title">{title}</h2><p>项目实施讨论 · 持续上下文 · {group.length > 1 ? `${group.length} Agents` : "单 Agent"}</p></div></div><div className="conversation-header-actions"><div className="agent-stack" data-testid="agent-avatar-stack" aria-label="当前会话 Agent">{group.slice(0, 3).map((agent) => <span key={agent}>{agent.slice(0, 1)}</span>)}{group.length > 3 && <span>+{group.length - 3}</span>}</div><button type="button" className="quiet" onClick={togglePause}>{paused ? "恢复" : "暂停"}</button><button type="button" className="quiet" aria-label="会话详情">⋯</button></div></header>
      <div className="conversation-source" data-testid="conversation-source">来源 · {source} · session {sessionId}</div>
      <div className={`conversation-status ${status.includes("执行中") ? "running" : ""}`} data-testid="conversation-status" aria-live="polite">{status}</div>
      <SequentialGraph state={sequential} profiles={modelProfiles} onApprove={approveOrchestration} />
      <Timeline entries={entries} group={group} provider={selectedProviderLabel} model={selectedModel} status={status} />
      <Composer onSend={send} onIntervene={intervene} pending={pending} paused={paused} model={selectedModel} providerId={selectedProviderId} modelOptions={modelOptions} onModelChange={(providerId, model) => { setSelectedProviderId(providerId); setSelectedModel(model); }} />
    </main>
    {canvasOpen ? <aside className="artifacts-canvas" aria-label="智能画布 · Artifacts"><header><strong>智能画布 · Artifacts</strong><button type="button" className="quiet" aria-label="折叠画布" onClick={() => setCanvasOpen(false)}>折叠</button></header>{htmlArtifact ? <HtmlArtifactPreview artifactId={htmlArtifact.artifactId} /> : <><nav aria-label="Artifacts 列表">{artifacts.map((item) => <button key={item.id} type="button" className="quiet" aria-pressed={artifactId === item.id} onClick={() => setArtifactId(item.id)}>{item.title}</button>)}</nav><section className="artifact-preview"><small>version v3 · {artifact.mimeType}</small><h3>{artifact.title}</h3><Renderer artifact={artifact} /></section></>}<section className="artifact-version-card"><strong>版本卡片 · Version cards</strong><p>当前结果 · 历史 Attempt · 审核证据</p></section></aside> : <button type="button" className="quiet canvas-reopen" aria-label="打开画布" onClick={() => setCanvasOpen(true)}>打开画布</button>}
  </section>;
}

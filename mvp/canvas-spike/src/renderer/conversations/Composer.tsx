import { useRef, useState } from "react";
import type React from "react";
import { ContextMenu } from "./ContextMenu";
import { MentionMenu } from "./MentionMenu";

type ModelOption = { id: string; providerId: string; model: string; label: string };
export type RuntimeOption = {
  selector: "python-term";
  label: string;
  selectable: boolean;
  trustStatus: "PRODUCTION_TRUSTED" | "DEV_UNTRUSTED" | null;
  reason: string | null;
};

export function Composer({ onSend, onIntervene, pending = false, paused = false, model = "default", providerId = "", modelOptions = [], onModelChange, runtime = "", runtimeOptions = [], onRuntimeChange }: { onSend: (prompt: string, contexts: string[]) => void; onIntervene?: (prompt: string) => void; pending?: boolean; paused?: boolean; model?: string; providerId?: string; modelOptions?: ModelOption[]; onModelChange?: (providerId: string, model: string) => void; runtime?: "" | "python-term"; runtimeOptions?: RuntimeOption[]; onRuntimeChange?: (runtime: "" | "python-term") => void }) {
  const [prompt, setPrompt] = useState("");
  const [contexts, setContexts] = useState<string[]>([]);
  const [contextOpen, setContextOpen] = useState(false);
  const [mentionOpen, setMentionOpen] = useState(false);
  const [pendingMentionAt, setPendingMentionAt] = useState<number | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const insertMention = (token: string) => {
    const input = inputRef.current;
    const cursor = input?.selectionStart ?? prompt.length;
    const hasPendingAt = pendingMentionAt !== null && prompt[pendingMentionAt] === "@";
    const start = hasPendingAt ? pendingMentionAt! : cursor;
    const end = hasPendingAt ? pendingMentionAt! + 1 : cursor;
    const next = `${prompt.slice(0, start)}${token}${prompt.slice(end)}`;
    setPrompt(next);
    setPendingMentionAt(null);
    setMentionOpen(false);
    requestAnimationFrame(() => { inputRef.current?.focus(); const position = start + token.length; inputRef.current?.setSelectionRange(position, position); });
  };
  const submit = () => {
    const value = prompt.trim();
    const selectedRuntime = runtime ? runtimeOptions.find((option) => option.selector === runtime) : undefined;
    if (!value || pending || (runtime !== "" && selectedRuntime?.selectable !== true)) return;
    onSend(value, contexts);
    setPrompt("");
    setContexts([]);
    setMentionOpen(false);
    setContextOpen(false);
    setPendingMentionAt(null);
  };
  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); return; }
    if (event.key === "@") { const cursor = inputRef.current?.selectionStart ?? prompt.length; setTimeout(() => { setPendingMentionAt(cursor); setMentionOpen(true); setContextOpen(false); }, 0); }
  };
  const selectedRuntimeOption = runtimeOptions.find((option) => option.selector === runtime);
  const runtimeSubmissionBlocked = runtime !== "" && selectedRuntimeOption?.selectable !== true;

  return <form className="v4-composer" aria-label="独立会话输入区" onSubmit={(event) => { event.preventDefault(); submit(); }}>
    {contexts.length > 0 && <div className="context-chips" data-testid="context-chips">
      {contexts.map((context) => <span className="context-chip" key={context}>{context}<button type="button" aria-label={`移除 ${context}`} onClick={() => setContexts((current) => current.filter((item) => item !== context))}>×</button></span>)}
    </div>}
    <textarea ref={inputRef} aria-label="会话消息" placeholder="发送消息… 使用 @ 指定 Agent、Skill 或 Tool" value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={handleKeyDown} rows={2} />
    <div className="composer-toolbar">
      <div className="composer-actions">
        <button type="button" className={`composer-icon ${contextOpen ? "active" : ""}`} aria-label="添加到本轮上下文" onClick={() => { setContextOpen((open) => !open); setMentionOpen(false); }}>＋</button>
        <button type="button" className={`composer-icon ${mentionOpen ? "active" : ""}`} aria-label="提及 Agent、Skill 或 Tool" onClick={() => { setMentionOpen((open) => !open); setContextOpen(false); setPendingMentionAt(null); }}>@</button>
        <small>Enter 发送 · Shift+Enter 换行</small>
      </div>
      <div className="composer-submit">
        {modelOptions.length > 0 ? <select className="model-select" aria-label="当前模型" value={`${providerId}/${model}`} onChange={(event) => { const [nextProvider, ...rest] = event.target.value.split("/"); onModelChange?.(nextProvider, rest.join("/")); }}>
          {modelOptions.map((option) => <option key={option.id} value={`${option.providerId}/${option.model}`}>{option.label}</option>)}
        </select> : <span className="model-badge" aria-label="当前模型">{model} <span>⌄</span></span>}
        <select className="runtime-select" aria-label="当前 Runtime" value={runtime} disabled={pending} onChange={(event) => onRuntimeChange?.(event.target.value as "" | "python-term")}>
          <option value="">默认运行路径</option>
          {runtimeOptions.map((option) => <option key={option.selector} value={option.selector} disabled={!option.selectable}>
            {option.label}{option.trustStatus ? ` · ${option.trustStatus}` : ""}{option.reason ? ` · ${option.reason}` : ""}
          </option>)}
        </select>
        {onIntervene && <button type="button" className="quiet" disabled={!prompt.trim() || paused} onClick={() => { const value = prompt.trim(); if (!value) return; onIntervene(value); setPrompt(""); }}>介入</button>}
        <button type="submit" className="send-button" aria-label="发送" disabled={pending || paused || runtimeSubmissionBlocked}>{pending ? "…" : "➤"}</button>
      </div>
    </div>
    {paused && <small className="composer-note">当前会话已暂停，恢复后才能发送新消息。</small>}
    <ContextMenu open={contextOpen} onSelect={(value) => { setContexts((current) => current.includes(value) ? current : [...current, value]); setContextOpen(false); }} />
    <MentionMenu open={mentionOpen} onSelect={insertMention} />
  </form>;
}

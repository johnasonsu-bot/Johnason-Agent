// Boundary doubles for source-only checks when upstream lib/ is not built.
// Keep event and frame shapes aligned with the pinned Session and Agent APIs.
import { randomUUID } from "node:crypto";

export const contexts = [];
let drive;
export function configureDriver(callback) {
  contexts.length = 0;
  drive = callback;
}

export const LlmRuntime = Symbol("llm");
export const SessionStore = Symbol("sessions");
export const SessionProjections = Symbol("sessionProjections");
export const SystemPrompt = Symbol("systemPrompt");
export const NativeToolExecutor = Symbol("native-tool-executor");
export const ToolRuntime = Symbol("tools");
export const AgentRegistry = Symbol("agents");
export const AgentLoop = Symbol("agentLoop");
export const SessionId = value => value;
export const MAX_TIMER_DELAY_MS = 2 ** 31 - 1;
export class LlmError extends Error {}
export const assertObjectJsonSchema = () => {};

export const createUserMessage = input => Object.freeze({
  ...input, id: randomUUID(), role: "user",
});
export const createAssistantMessage = input => Object.freeze({
  ...input, id: randomUUID(), role: "assistant", source: { kind: "model", ...input.source },
});
export const createSystemMessage = text => Object.freeze({
  id: randomUUID(), role: "system", source: { kind: "system-prompt" },
  content: text.length === 0 ? [] : [{ type: "text", text }],
});
export const isReplacementSurfaceEvent = event => typeof event.surfaceOp === "object";

export class Session {
  #log = [];
  static create(id) { return new Session(id); }
  constructor(id) { this.id = id; this.surface = { nodes: [] }; }
  eventAt(seq) { return this.#log[seq]; }
  snapshotEvents(fromSeq = 0, toSeqExclusive = this.#log.length) {
    return Object.freeze(this.#log.slice(fromSeq, toSeqExclusive));
  }
  append(type, data, options = {}) {
    const event = { seq: this.#log.length, time: Date.now(), type, data, ...options };
    this.#log.push(event);
    if (options.surfaceOp === "append") this.surface.nodes.push(event.seq);
    else if (options.surfaceOp?.op === "replace") {
      const first = this.surface.nodes.indexOf(options.surfaceOp.startSeq);
      const last = this.surface.nodes.indexOf(options.surfaceOp.endSeq);
      this.surface.nodes.splice(first, last - first + 1, event.seq);
    }
    return event;
  }
}

export class DeepSeekAdapter {
  constructor(connection) {
    if (typeof connection.prepareExtensions !== "function") {
      throw new Error("DeepSeekAdapter requires the request-extension preparation contract");
    }
    this.connection = connection;
  }
}
export function resolveAdapterOptions(config) {
  // The pinned upstream adapter has a Messages-only wire contract.
  return { ...config, protocol: config.protocol ?? "messages" };
}

export class Context {
  constructor() {
    contexts.push(this);
    this.plugins = [];
    this.listeners = new Map();
    this.sections = [];
    this.contextItems = [];
    this.disposed = false;
    this.fiber = { dispose: async () => { this.disposed = true; } };
  }
  async plugin(plugin, config) {
    this.plugins.push(plugin);
    if (plugin === LlmRuntime) {
      this.llm = { registerAdapter: (routes, adapter) => { this.adapter = adapter; } };
    } else if (plugin === SessionStore) {
      this.sessions = { create: id => Session.create(id) };
    } else if (plugin === SystemPrompt) {
      this.promptConfig = config;
      this.systemPrompt = {
        section: section => this.sections.push(section),
        context: item => this.contextItems.push(item),
      };
    } else if (plugin === AgentLoop) {
      this.agentLoop = {
        createAgent: async (owner, options) => {
          this.createOptions = options;
          const session = Session.create(options.sessionId);
          for (const { type, data, surfaceOp, sourceEventSeqs } of options.seed) {
            session.append(type, data, {
              ...(surfaceOp === undefined ? {} : { surfaceOp }),
              ...(sourceEventSeqs === undefined ? {} : { sourceEventSeqs }),
            });
          }
          this.agent = {
            session,
            followup: message => { this.currentMessage = message; },
            cancel: cause => { this.cancelCause = cause; },
            whenIdle: async () => { await drive(this); },
          };
          return { agent: this.agent };
        },
      };
    }
  }
  on(type, callback) {
    const callbacks = this.listeners.get(type) ?? [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }
  emit(type, ...args) {
    for (const callback of this.listeners.get(type) ?? []) callback(...args);
  }
  persist(type, data, options) {
    const event = this.agent.session.append(type, data, options);
    this.emit("session/event", this.agent.session, event);
    return event;
  }
}

export function completeTurn(ctx, text, stream = []) {
  const message = ctx.persist("assistant/message", {
    turn: 1, step: 1, stream,
    message: createAssistantMessage({
      content: [{ type: "text", text }],
      source: { provider: "deepseek-official", model: "source-fixture" },
    }),
  }, { surfaceOp: "append" });
  ctx.persist("step/end", { turn: 1, step: 1 });
  ctx.persist("turn/end", { turn: 1, reason: { kind: "completed" } });
  return message;
}

export function emitChunks(ctx) {
  const base = { attemptId: `${ctx.agent.session.id}:1` };
  ctx.persist("turn/start", { turn: 1 });
  ctx.persist("step/start", { turn: 1, step: 1 });
  ctx.emit("agent/assistant-stream", {
    agent: ctx.agent, frame: { ...base, type: "start", revision: 1, turn: 1, step: 1 },
  });
  const chunks = [
    { type: "reasoning-delta", index: 0, text: "Provider reasoning" },
    { type: "text-delta", index: 1, text: "live " },
    { type: "text-delta", index: 1, text: "answer" },
  ];
  const time = Date.now();
  chunks.forEach((chunk, index) => ctx.emit("agent/assistant-stream", {
    agent: ctx.agent,
    frame: { ...base, type: "chunk", revision: index + 2, index, time, chunk },
  }));
  const message = completeTurn(ctx, "live answer", [
    { type: "reasoning-chunks", time0: time, index: 0, dt: [], texts: ["Provider reasoning"] },
    { type: "text-chunks", time0: time, index: 1, dt: [0], texts: ["live ", "answer"] },
  ]);
  ctx.emit("agent/assistant-stream", {
    agent: ctx.agent,
    frame: { ...base, type: "end", revision: 5, index: 3,
      outcome: { kind: "committed", eventType: "assistant/message", seq: message.seq } },
  });
}

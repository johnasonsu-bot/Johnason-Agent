import { Context } from "../../../../third_party/deepseek-harness/vendor/cordis/lib/index.js";
import AgentLoop from "../../../../third_party/deepseek-harness/packages/core/agent-loop/lib/index.js";
import AgentRegistry from "../../../../third_party/deepseek-harness/packages/core/agent/lib/index.js";
import SessionStore, {
  Session,
  SessionId,
} from "../../../../third_party/deepseek-harness/packages/core/session/lib/index.js";
import SystemPrompt from "../../../../third_party/deepseek-harness/packages/core/system-prompt/lib/index.js";
import ToolRuntime, {
  assertObjectJsonSchema,
} from "../../../../third_party/deepseek-harness/packages/core/tools/lib/index.js";
import LlmRuntime, {
  LlmError,
  createAssistantMessage,
  createUserMessage,
} from "../../../../third_party/deepseek-harness/packages/llm/llm/lib/index.js";
import {
  DeepSeekAdapter,
  resolveAdapterOptions,
} from "../../../../third_party/deepseek-harness/packages/llm/llm-deepseek/lib/index.js";
import {
  MAX_TIMER_DELAY_MS,
} from "../../../../third_party/deepseek-harness/packages/util/timeout/lib/index.js";

const PLATFORM_RUN_IDENTITY_FIELDS = Object.freeze([
  "session_id", "run_id", "term_id", "step_id", "command_id", "agent_id", "agent_role",
]);

export class DshSessionFailure extends Error {
  constructor(failure, stage) {
    super("DSH Session failed");
    const provider = failure && typeof failure.code === "string"
      && ["TRANSPORT", "AUTH", "RATE_LIMIT", "HTTP_ERROR", "INVALID_REQUEST",
        "SERVER", "SERVER_ERROR", "EMPTY_RESPONSE", "MALFORMED_RESPONSE", "STREAM_CLOSED"].includes(failure.code)
      || [400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504].includes(failure?.status);
    this.publicFailure = Object.freeze({
      reason_code: provider ? "provider_request_failed" : "runtime_verification_failed",
      failure_stage: provider ? "provider_request" : stage,
      ...([400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504].includes(failure?.status)
        ? { http_status: failure.status } : {}),
    });
  }
}


function messageText(message) {
  return message.content
    .filter(block => block.type === "text")
    .map(block => block.text)
    .join("");
}


function freezeJson(value) {
  if (Array.isArray(value)) {
    for (const item of value) freezeJson(item);
  } else if (value !== null && typeof value === "object") {
    for (const item of Object.values(value)) freezeJson(item);
  }
  return Object.freeze(value);
}


function platformFailureMessage(output) {
  if (typeof output === "string" && output.length !== 0) return output;
  if (output !== null && typeof output === "object"
      && typeof output.message === "string" && output.message.length !== 0) {
    return output.message;
  }
  return "platform tool failed";
}


/**
 * Register only the platform-supplied RunEnvelope tool manifest with DSH's
 * native ToolRuntime. The injected callback remains the sole operation
 * boundary: this adapter never reads files, runs commands, or creates effects.
 */
export function registerPlatformTools(ctx, {
  toolManifest,
  runIdentity,
  executeTool,
}) {
  if (!Array.isArray(toolManifest) || runIdentity === null
      || typeof runIdentity !== "object" || typeof executeTool !== "function") {
    throw new Error("DSH platform tool adapter configuration is invalid");
  }
  if (Object.keys(runIdentity).sort().join(",")
        !== [...PLATFORM_RUN_IDENTITY_FIELDS].sort().join(",")
      || PLATFORM_RUN_IDENTITY_FIELDS.some(
        field => typeof runIdentity[field] !== "string" || runIdentity[field].length === 0,
      )) {
    throw new Error("DSH platform tool run identity is invalid");
  }
  const frozenIdentity = freezeJson(structuredClone(runIdentity));
  const frozenManifest = freezeJson(structuredClone(toolManifest));
  const toolIds = new Set();
  for (const manifest of frozenManifest) {
    if (!Number.isInteger(manifest?.timeout_ms) || manifest.timeout_ms <= 0
        || manifest.timeout_ms > MAX_TIMER_DELAY_MS) {
      throw new Error(
        `DSH platform tool timeout_ms must be a positive integer no greater than ${MAX_TIMER_DELAY_MS}`,
      );
    }
    if (manifest === null || typeof manifest !== "object"
        || typeof manifest.tool_id !== "string" || manifest.tool_id.length === 0
        || manifest.schema === null || typeof manifest.schema !== "object"
        || Array.isArray(manifest.schema)
        || typeof manifest.version !== "string" || manifest.version.length === 0
        || typeof manifest.read_only !== "boolean"
        || !["idempotent", "non_idempotent"].includes(manifest.idempotency)) {
      throw new Error("DSH platform tool manifest entry is invalid");
    }
    assertObjectJsonSchema(manifest.schema);
    if (toolIds.has(manifest.tool_id)) {
      throw new Error(`DSH platform tool manifest contains duplicate tool_id ${manifest.tool_id}`);
    }
    toolIds.add(manifest.tool_id);
  }
  const disposers = [];
  const disposeAll = () => {
    while (disposers.length !== 0) {
      const dispose = disposers.pop();
      dispose();
    }
  };
  try {
    for (const manifest of frozenManifest) {
      disposers.push(ctx.tools.register({
        name: manifest.tool_id,
        description: `Platform tool ${manifest.tool_id} (version ${manifest.version})`,
        parameters: manifest.schema,
        timeoutMs: manifest.timeout_ms,
        ...(manifest.read_only && manifest.idempotency === "idempotent"
          ? { isConcurrencySafe: () => true } : {}),
        output: {
          schema: {
            type: "object",
            properties: {
              output: {},
              effect_id: { type: "string" },
            },
            required: ["output"],
            additionalProperties: false,
          },
          render(_arguments, value) {
            return [{ type: "text", text: JSON.stringify(value) }];
          },
        },
        async execute(arguments_, exec) {
          const timeout = new AbortController();
          const timer = setTimeout(() => {
            timeout.abort(new Error(`platform tool ${manifest.tool_id} timed out`));
          }, manifest.timeout_ms);
          const signal = AbortSignal.any([exec.signal, timeout.signal]);
          try {
            const response = await executeTool(Object.freeze({
              tool_call_id: String(exec.callId),
              tool_id: manifest.tool_id,
              arguments: arguments_,
              identity: frozenIdentity,
              manifest,
              signal,
            }));
            if (signal.aborted) throw signal.reason ?? new Error("platform tool was cancelled");
            if (response === null || typeof response !== "object" || !("output" in response)) {
              throw new Error("platform tool returned an invalid result");
            }
            if (response.status === "failed") {
              throw new Error(platformFailureMessage(response.output));
            }
            if (response.status !== "completed") {
              throw new Error("platform tool returned an invalid result");
            }
            const effectId = response.effect_id;
            if (!manifest.read_only && (typeof effectId !== "string" || effectId.length === 0)) {
              throw new Error("platform write result is unconfirmed: effect_id is required");
            }
            if (effectId !== undefined && (typeof effectId !== "string" || effectId.length === 0)) {
              throw new Error("platform tool returned an invalid effect_id");
            }
            return {
              output: response.output,
              ...(effectId !== undefined ? { effect_id: effectId } : {}),
            };
          } finally {
            clearTimeout(timer);
          }
        },
      }));
    }
  } catch (error) {
    disposeAll();
    throw error;
  }
  return disposeAll;
}


function historySeed(messages, provider, model, sessionId) {
  const seed = Session.create(SessionId(`${sessionId}:seed`));
  let turn = 0;
  for (const message of messages) {
    if (message.role === "user") {
      seed.append("user/message", createUserMessage({
        content: [{ type: "text", text: message.content }],
        source: { kind: "user" },
      }), { surfaceOp: "append" });
      continue;
    }
    if (message.role !== "assistant") {
      throw new Error("DSH native Session history supports user and assistant roles only");
    }
    turn += 1;
    seed.append("turn/start", { turn });
    seed.append("step/start", { turn, step: 1 });
    seed.append("assistant/message", {
      turn,
      step: 1,
      message: createAssistantMessage({
        content: [{ type: "text", text: message.content }],
        source: { provider, model },
      }),
    }, { surfaceOp: "append" });
    seed.append("step/end", { turn, step: 1 });
    seed.append("turn/end", { turn, reason: { kind: "completed" } });
  }
  return seed.events;
}


function registerOrderedInput(ctx, materialized) {
  let order = 0;
  for (const section of materialized.promptSections) {
    ctx.systemPrompt.section({
      name: `host:prompt:${section.section_id}`,
      order: order++,
      text: section.content,
    });
  }
  for (const message of materialized.messages.filter(item => item.role === "system")) {
    ctx.systemPrompt.section({
      name: `host:system:${message.message_id}`,
      order: order++,
      text: message.content,
    });
  }
  for (const item of materialized.contextItems) {
    ctx.systemPrompt.context({
      name: `host:context:${item.item_id}`,
      order: order++,
      text: item.content,
    });
  }
}


function realProviderAdapter(provider, credential) {
  const route = provider.route;
  if (route.protocol !== "deepseek") {
    throw new Error("DSH upstream adapter requires the deepseek protocol");
  }
  if (route.metadata_headers.length !== 0) {
    throw new Error("DSH upstream adapter does not accept custom metadata headers");
  }
  const connection = resolveAdapterOptions({
    baseURL: route.base_url,
    thinking: route.thinking_enabled ? "enabled" : "disabled",
    reasoningEffort: route.thinking_enabled ? route.reasoning_effort : "off",
    models: [{ id: provider.model, name: provider.model }],
  });
  return new DeepSeekAdapter({
    options: () => connection,
    resolveApiKey: () => Promise.resolve(credential()),
    resolveUserId: () => "00000000-0000-4000-8000-000000000001",
  });
}


export async function runDeepSeekHarnessSession({
  materialized,
  provider,
  credential,
  sessionId,
  signal,
  toolManifest = [],
  runIdentity,
  executeTool,
  onEvent = () => {},
  adapter = realProviderAdapter,
}) {
  if (signal.aborted) throw signal.reason ?? new Error("DSH Session was cancelled");
  const messages = materialized.messages.filter(message => message.role !== "system");
  const current = messages.at(-1);
  if (current?.role !== "user") {
    throw new Error("DSH native Session requires the current message to be user-authored");
  }
  const history = messages.slice(0, -1);
  const route = "deepseek-official";
  const ctx = new Context();
  let agent;
  let abort;
  let content = "";
  let stage = "session_setup";
  try {
    await ctx.plugin(LlmRuntime);
    await ctx.plugin(SessionStore);
    await ctx.plugin(SystemPrompt, {
      includeHarnessIdentity: false,
      includeRuntimeContext: true,
      persona: "",
    });
    await ctx.plugin(ToolRuntime);
    if (toolManifest.length !== 0) {
      registerPlatformTools(ctx, { toolManifest, runIdentity, executeTool });
    }
    await ctx.plugin(AgentRegistry);
    await ctx.plugin(AgentLoop, { agents: [] });
    registerOrderedInput(ctx, materialized);
    ctx.llm.registerAdapter([route], adapter(provider, credential));
    ctx.on("session/event", (_session, event) => {
      onEvent(event);
      if (event.type === "assistant/chunk"
          && event.data?.chunk?.type === "text-delta") {
        content += event.data.chunk.text;
      }
      if (event.type === "assistant/message") {
        content = messageText(event.data.message);
      }
    });
    const published = await ctx.agentLoop.createAgent(ctx, {
      sessionId: SessionId(sessionId),
      seed: historySeed(history, route, provider.model, sessionId),
      signal,
      agentOptions: {
        provider: route,
        model: provider.model,
        reasoningEffort: provider.route.thinking_enabled
          ? provider.route.reasoning_effort
          : "off",
      },
    });
    agent = published.agent;
    abort = () => agent.cancel({ kind: "user" });
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) {
      abort();
      throw signal.reason ?? new Error("DSH Session was cancelled");
    }
    stage = "session_execution";
    agent.followup(createUserMessage({
      content: [{ type: "text", text: current.content }],
      source: { kind: "user" },
    }));
    await agent.whenIdle();
    const terminal = agent.session.events.findLast(event => event.type === "turn/end");
    const reason = terminal?.data?.reason?.kind;
    if (!terminal || !["completed", "max-tokens"].includes(reason)) {
      throw new Error(`DSH native Session ended with ${reason ?? "no terminal"}`);
    }
    return Object.freeze({ content, nativeEvents: agent.session.events });
  } catch (error) {
    const terminal = agent?.session.events.findLast(event => event.type === "turn/end");
    throw new DshSessionFailure(
      error instanceof LlmError ? error.failure : terminal?.data?.reason?.error,
      stage,
    );
  } finally {
    if (abort !== undefined) signal.removeEventListener("abort", abort);
    await ctx.fiber.dispose();
  }
}

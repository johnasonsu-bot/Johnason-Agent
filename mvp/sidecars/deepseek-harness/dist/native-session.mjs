import { Context } from "../../../../third_party/deepseek-harness/vendor/cordis/lib/index.js";
import AgentLoop from "../../../../third_party/deepseek-harness/packages/core/agent-loop/lib/index.js";
import AgentRegistry from "../../../../third_party/deepseek-harness/packages/core/agent/lib/index.js";
import SessionStore, {
  Session,
  SessionId,
} from "../../../../third_party/deepseek-harness/packages/core/session/lib/index.js";
import SystemPrompt from "../../../../third_party/deepseek-harness/packages/core/system-prompt/lib/index.js";
import ToolRuntime from "../../../../third_party/deepseek-harness/packages/core/tools/lib/index.js";
import LlmRuntime, {
  createAssistantMessage,
  createUserMessage,
} from "../../../../third_party/deepseek-harness/packages/llm/llm/lib/index.js";
import {
  DeepSeekAdapter,
  resolveAdapterOptions,
} from "../../../../third_party/deepseek-harness/packages/llm/llm-deepseek/lib/index.js";


function messageText(message) {
  return message.content
    .filter(block => block.type === "text")
    .map(block => block.text)
    .join("");
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
  try {
    await ctx.plugin(LlmRuntime);
    await ctx.plugin(SessionStore);
    await ctx.plugin(SystemPrompt, {
      includeHarnessIdentity: false,
      includeRuntimeContext: true,
      persona: "",
    });
    await ctx.plugin(ToolRuntime);
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
      agentOptions: {
        provider: route,
        model: provider.model,
        reasoningEffort: provider.route.thinking_enabled
          ? provider.route.reasoning_effort
          : "off",
      },
    });
    agent = published.agent;
    abort = () => agent.cancel(signal.reason ?? new Error("DSH Session was cancelled"));
    signal.addEventListener("abort", abort, { once: true });
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
  } finally {
    if (abort !== undefined) signal.removeEventListener("abort", abort);
    await ctx.fiber.dispose();
  }
}

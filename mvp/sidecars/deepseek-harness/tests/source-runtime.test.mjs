// No builds, installs, HTTP servers or model requests:
// node --experimental-strip-types --no-warnings --test tests/source-runtime.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks, stripTypeScriptTypes } from "node:module";
import test from "node:test";
import {
  AgentLoop, SessionProjections, contexts, configureDriver, completeTurn, emitChunks,
  createAssistantMessage,
} from "./fixtures/source-runtime.mjs";

const fixtureUrl = new URL("./fixtures/source-runtime.mjs", import.meta.url).href;
const exportsByPath = new Map([
  ["vendor/cordis/lib/index.js", "Context"],
  ["packages/core/agent-loop/lib/index.js", "AgentLoop as default"],
  ["packages/core/agent/lib/index.js", "AgentRegistry as default"],
  ["packages/core/session/lib/index.js", "SessionStore as default, Session, SessionId"],
  ["packages/session/session-projection/lib/index.js", "SessionProjections as default"],
  ["packages/core/system-prompt/lib/index.js", "SystemPrompt as default"],
  ["packages/core/tool-executor-native/lib/index.js", "NativeToolExecutor"],
  ["packages/core/tools/lib/index.js", "ToolRuntime as default, assertObjectJsonSchema"],
  ["packages/llm/llm/lib/index.js", "LlmRuntime as default, LlmError, createAssistantMessage, createUserMessage, createSystemMessage"],
  ["packages/llm/llm-deepseek/lib/index.js", "DeepSeekAdapter, resolveAdapterOptions"],
  ["packages/util/timeout/lib/index.js", "MAX_TIMER_DELAY_MS"],
]);
const hook = registerHooks({
  resolve(specifier, context, nextResolve) {
    const key = specifier.split("/third_party/deepseek-harness/")[1];
    if (!exportsByPath.has(key)) return nextResolve(specifier, context);
    assert.ok(context.parentURL.endsWith("/src/platform-runtime.ts"), "upstream imports must cross the real platform boundary");
    return { url: `data:text/javascript,${encodeURIComponent(
      `export { ${exportsByPath.get(key)} } from ${JSON.stringify(fixtureUrl)};`,
    )}`, shortCircuit: true };
  },
});

let runDeepSeekHarnessSession, createSidecar;
try {
  ({ runDeepSeekHarnessSession } = await import("../src/native-session.ts"));
  ({ createSidecar } = await import("../src/server.ts"));
} finally {
  hook.deregister();
}

// Exercise the pinned upstream's pure restoration and prompt projection logic,
// resolving source in memory only; no generated lib files or native services.
const upstream = new URL("../../../../third_party/deepseek-harness/", import.meta.url);
const sourceExports = new Map([
  ["@deepseek-ai/dsh-session-format", "packages/session/session-format/src/index.ts"],
  ["@deepseek-ai/dsh-session-format-v0-to-v1", "packages/session/session-format-v0-to-v1/src/index.ts"],
  ["@deepseek-ai/dsh-util-values", "packages/util/values/src/index.ts"],
]);
const sourceHook = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (sourceExports.has(specifier)) {
      return { url: new URL(sourceExports.get(specifier), upstream).href, shortCircuit: true };
    }
    let source;
    if (specifier === "@deepseek-ai/dsh-session-format-v1-to-v2") {
      source = ["validation", "dispositions"].map(name =>
        `export * from ${JSON.stringify(new URL(`packages/session/session-format-v1-to-v2/src/${name}.ts`, upstream).href)};`,
      ).join("\n");
    } else if (specifier === "@deepseek-ai/dsh-llm") {
      source = `export { createSystemMessage, createUserMessage } from ${JSON.stringify(fixtureUrl)};`;
    } else if (specifier === "@deepseek-ai/dsh-session") {
      source = `export { isReplacementSurfaceEvent } from ${JSON.stringify(fixtureUrl)};`;
    }
    return source === undefined ? nextResolve(specifier, context)
      : { url: `data:text/javascript,${encodeURIComponent(source)}`, shortCircuit: true };
  },
  load(url, context, nextLoad) {
    if (url.startsWith(upstream.href) && url.endsWith(".ts")) {
      return { format: "module", source: stripTypeScriptTypes(readFileSync(new URL(url), "utf8"), {
        mode: "transform", sourceUrl: url,
      }), shortCircuit: true };
    }
    return nextLoad(url, context);
  },
});
let restoreReleasedV4Artifact, SystemPromptProjection, KNOWN_SESSION_EVENT_TYPES;
try {
  ({ KNOWN_SESSION_EVENT_TYPES } = await import(new URL("packages/core/session/src/known-event-types.ts", upstream)));
  ({ restoreReleasedV4Artifact } = await import(new URL(
    "packages/session/session-format-v3-to-v4/src/validation.ts", upstream,
  )));
  ({ SystemPromptProjection } = await import(new URL(
    "packages/core/agent-loop/src/runtime-context.ts", upstream,
  )));
} finally {
  sourceHook.deregister();
}

function restoreV4(events) {
  return restoreReleasedV4Artifact({
    header: { version: 4, id: "source-session", createdAt: 1, isSeeded: false, delegationDepth: 0 },
    inheritedEventCount: 0, events,
  }, KNOWN_SESSION_EVENT_TYPES);
}

test("historical turns restore as V4 and keep the current system prompt ahead of prior messages", async () => {
  const history = [
    { role: "user", content: "First question" },
    { role: "assistant", content: "First answer" },
    { role: "user", content: "Second question" },
    { role: "assistant", content: "Second answer" },
  ];
  let request;
  configureDriver(ctx => {
    restoreV4(ctx.createOptions.seed);
    const turn = ctx.agent.session.snapshotEvents().findLast(event => event.type === "turn/end").data.turn + 1;
    ctx.persist("turn/start", { turn });
    ctx.persist("step/start", { turn, step: 1 });
    const rendered = ctx.sections.map(section => section.text).filter(Boolean).join("\n\n");
    const projection = new SystemPromptProjection(ctx.agent.session);
    const model = ctx.adapter.connection.options().models.find(entry => entry.id === input.provider.model);
    const inHistory = model?.systemPromptUpdate === "in-history";
    assert.equal(inHistory, false, "the host route must normalize the system head for its first request");
    for (const { message, intent } of projection.project(rendered, { inHistory, startsSeries: false })) {
      ctx.persist("system/message", { turn, step: 1, message }, intent);
    }
    ctx.persist("user/message", ctx.currentMessage, { surfaceOp: "append" });
    request = ctx.agent.session.surface.nodes.map(seq => {
      const event = ctx.agent.session.eventAt(seq);
      return event.type === "user/message" ? event.data : event.data.message;
    });
    ctx.persist("assistant/message", { turn, step: 1, stream: [], message: createAssistantMessage({
      content: [{ type: "text", text: "Current answer" }],
      source: { provider: "deepseek-official", model: "source-fixture" },
    }) }, { surfaceOp: "append" });
    ctx.persist("step/end", { turn, step: 1 });
    ctx.persist("turn/end", { turn, reason: { kind: "completed" } });
  });
  const input = sessionInput();
  input.materialized.messages.unshift({ role: "system", message_id: "policy", content: "Host policy" }, ...history);
  const result = await runDeepSeekHarnessSession(input);
  assert.doesNotThrow(() => restoreV4(result.nativeEvents));
  assert.deepEqual(request.map(message => message.role), ["system", "user", "assistant", "user", "assistant", "user"]);
  assert.deepEqual(request.map(message => message.content[0].text), [
    "Platform instruction\n\nHost policy", ...history.map(message => message.content), "Current question",
  ]);
  const head = contexts[0].createOptions.seed.find(event => event.surfaceOp === "append");
  assert.equal(head.type, "system/message");
  assert.deepEqual(head.data.message.content, []);
});

test("user-only seed reserves a balanced system head and empty history leaves initialization to the loop", async () => {
  configureDriver(ctx => completeTurn(ctx, "answer"));
  const input = sessionInput();
  input.materialized.messages.unshift({ role: "user", content: "Earlier context" });
  await runDeepSeekHarnessSession(input);
  const events = contexts[0].createOptions.seed;
  assert.doesNotThrow(() => restoreV4(events));
  assert.equal(events.find(event => event.surfaceOp === "append").type, "system/message");
  assert.equal(events.at(-1).type, "turn/end");
  configureDriver(ctx => completeTurn(ctx, "answer"));
  await runDeepSeekHarnessSession(sessionInput());
  assert.deepEqual(contexts[0].createOptions.seed, []);
});

function sessionInput(overrides = {}) {
  return {
    materialized: {
      promptSections: [{ section_id: "first", content: "Platform instruction" }],
      contextItems: [{ item_id: "context", content: "Platform context" }],
      messages: [{ role: "user", content: "Current question" }],
    },
    provider: { model: "source-fixture", route: {
      protocol: "deepseek", base_url: "https://example.invalid",
      metadata_headers: [], thinking_enabled: false, reasoning_effort: "off",
    } },
    credential: () => "source-fixture-only",
    sessionId: "source-session", signal: new AbortController().signal,
    ...overrides,
  };
}

test("native composition supplies session projections and uses the official Messages routing", async () => {
  configureDriver(ctx => completeTurn(ctx, "answer"));
  await runDeepSeekHarnessSession(sessionInput());
  const ctx = contexts[0];
  assert.ok(ctx.plugins.includes(SessionProjections));
  assert.ok(ctx.plugins.indexOf(SessionProjections) < ctx.plugins.indexOf(AgentLoop));
  assert.equal(ctx.adapter.connection.options().protocol, "messages");
  assert.equal(ctx.adapter.connection.options().baseURL, "https://example.invalid");
  const extensions = await ctx.adapter.connection.prepareExtensions({ body: {} });
  assert.deepEqual(extensions.fields, {});
  assert.equal(typeof extensions.accept, "function");
  await extensions.accept();
  assert.equal(ctx.promptConfig.personaPrefix, "");
  assert.equal(ctx.sections[0].text, "Platform instruction");
  assert.equal(ctx.contextItems[0].text, "Platform context");
  assert.equal(ctx.disposed, true);
});

test("native transient chunks reach their callback without becoming durable session events", async () => {
  configureDriver(emitChunks);
  const chunks = [], events = [];
  const result = await runDeepSeekHarnessSession(sessionInput({
    onChunk: chunk => chunks.push(chunk), onEvent: event => events.push(event),
  }));
  assert.deepEqual(chunks, [
    { type: "reasoning-delta", index: 0, text: "Provider reasoning" },
    { type: "text-delta", index: 1, text: "live " },
    { type: "text-delta", index: 1, text: "answer" },
  ]);
  assert.deepEqual(events.map(event => event.type), [
    "turn/start", "step/start", "assistant/message", "step/end", "turn/end",
  ]);
  assert.equal(result.content, "live answer");
  assert.deepEqual(result.nativeEvents, events);
  assert.ok(Object.isFrozen(result.nativeEvents));
  assert.equal("events" in contexts[0].agent.session, false);
});

test("native stream callback ignores publications owned by a different agent", async () => {
  configureDriver(ctx => {
    ctx.emit("agent/assistant-stream", { agent: {
      ...ctx.agent, session: { ...ctx.agent.session, id: "other-session" },
    }, frame: {
      type: "chunk", attemptId: "other:1", revision: 1, index: 0, time: Date.now(),
      chunk: { type: "text-delta", index: 0, text: "Other agent output" },
    } });
    completeTurn(ctx, "Owned result");
  });
  const chunks = [];
  const result = await runDeepSeekHarnessSession(sessionInput({ onChunk: chunk => chunks.push(chunk) }));
  assert.deepEqual(chunks, []);
  assert.equal(result.content, "Owned result");
});

test("seeded assistant history carries the required compact stream field", async () => {
  configureDriver(ctx => completeTurn(ctx, "answer"));
  const input = sessionInput();
  input.materialized.messages.unshift(
    { role: "user", content: "Earlier question" },
    { role: "assistant", content: "Earlier answer" },
  );
  await runDeepSeekHarnessSession(input);
  const history = contexts[0].createOptions.seed;
  const seeded = history.find(event => event.type === "assistant/message");
  assert.deepEqual(seeded.data.stream, []);
  assert.equal(seeded.data.message.content[0].text, "Earlier answer");
  assert.equal(contexts[0].currentMessage.content[0].text, "Current question");
});

function modelQuery(options = {}) {
  const { payload } = JSON.parse(readFileSync(new URL("./runtime-query-v2-fixture.json", import.meta.url)));
  payload.envelope.provider_ref = "provider-profile:source-contract";
  payload.envelope.tool_manifest = [];
  const provider = sessionInput().provider;
  const sidecar = createSidecar({
    runtimeId: "dsh", buildId: "dsh:model-host-v2-r1",
    grantChannel: { consumeForEnvelope: envelope => ({
      acknowledgement: envelope, provider, secret: Buffer.from("source-fixture-only"),
    }) },
    ...options,
  });
  return { sidecar, payload };
}

test("Host rejects an incompatible adapter before consuming a provider grant", () => {
  let consumed = false;
  assert.throws(() => modelQuery({
    grantChannel: { consumeForEnvelope() { consumed = true; } },
    nativeAdapter: { contractVersion: 2, capabilities: {}, run() {} },
  }), /native adapter/);
  assert.equal(consumed, false);
});

test("Host invokes the explicit adapter contract and preserves conservative capabilities", async () => {
  const { NATIVE_ADAPTER_CAPABILITIES } = await import("../src/platform-contract.ts");
  let input;
  const { sidecar, payload } = modelQuery({ nativeAdapter: {
    contractVersion: 1, capabilities: NATIVE_ADAPTER_CAPABILITIES,
    async run(value) { input = value; return { content: "adapter result", nativeEvents: [] }; },
  } });
  const events = [];
  await sidecar.startQuery(payload, event => events.push(event)).completion;
  assert.equal(input.sessionId, payload.envelope.session_id);
  assert.equal(typeof input.credential, "function");
  assert.equal(typeof input.executeTool, "function");
  assert.ok(input.signal instanceof AbortSignal);
  assert.equal(events[0].payload.content, "adapter result");
  for (const flag of ["tools", "plugins", "workspace", "pause_resume", "checkpoints"]) assert.equal(sidecar.capabilities()[flag], false);
});

test("Host v2 maps transient text chunks to ordered output deltas before completion", async () => {
  configureDriver(emitChunks);
  const { sidecar, payload } = modelQuery();
  const emitted = [];
  const started = sidecar.startQuery(payload, event => emitted.push(event));
  await started.completion;
  assert.deepEqual(emitted.map(event => event.type), [
    "assistant.delta", "assistant.delta", "assistant.message", "runtime.status",
  ]);
  assert.deepEqual(emitted.slice(0, 3).map(event => event.payload.content), [
    "live ", "answer", "live answer",
  ]);
  assert.deepEqual(emitted.at(-1).payload, { status: "completed" });
  assert.deepEqual([...started.events, ...emitted].map(event => event.cursor), [1, 2, 3, 4, 5]);
});

test("Host v2 cancellation waits for the native session to settle and disposes its context", async () => {
  const entered = Promise.withResolvers();
  configureDriver(async ctx => {
    ctx.persist("turn/start", { turn: 1 });
    entered.resolve(ctx);
    await new Promise(resolve => ctx.createOptions.signal.addEventListener("abort", resolve, { once: true }));
    ctx.persist("turn/end", { turn: 1, reason: { kind: "aborted", reason: { kind: "user" } } });
  });
  const { sidecar, payload } = modelQuery();
  const emitted = [];
  const started = sidecar.startQuery(payload, event => emitted.push(event));
  const ctx = await entered.promise;
  const cancelled = await sidecar.cancel(payload.envelope.run_id);
  await started.completion;
  assert.deepEqual(ctx.cancelCause, { kind: "user" });
  assert.equal(ctx.disposed, true);
  assert.deepEqual(cancelled.payload, { status: "cancelled" });
  assert.deepEqual(emitted, []);
  assert.throws(() => sidecar.checkpoint(), /unavailable/);
});

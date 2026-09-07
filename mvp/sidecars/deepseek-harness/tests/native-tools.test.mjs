import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";

import { Context } from "../../../../third_party/deepseek-harness/vendor/cordis/lib/index.js";
import SystemPrompt from "../../../../third_party/deepseek-harness/packages/core/system-prompt/lib/index.js";
import ToolRuntime from "../../../../third_party/deepseek-harness/packages/core/tools/lib/index.js";

import {
  DshSessionFailure,
  registerPlatformTools,
  runDeepSeekHarnessSession,
} from "../src/native-session.ts";


async function toolContext() {
  const ctx = new Context();
  await ctx.plugin(SystemPrompt, {
    includeHarnessIdentity: false,
    includeRuntimeContext: false,
    persona: "",
  });
  await ctx.plugin(ToolRuntime);
  return ctx;
}


async function nativeTools(options) {
  const ctx = await toolContext();
  registerPlatformTools(ctx, options);
  return ctx;
}


const IDENTITY = {
  session_id: "session-native-tools",
  run_id: "run-native-tools",
  term_id: "term-native-tools",
  step_id: "step-native-tools",
  command_id: "command-native-tools",
  agent_id: "agent-native-tools",
  agent_role: "researcher",
};


function manifestEntry(overrides = {}) {
  return {
    tool_id: "platform_lookup",
    schema: { type: "object" },
    version: "1",
    read_only: true,
    timeout_ms: 250,
    idempotency: "idempotent",
    ...overrides,
  };
}


function toolCallStream(callId, name, arguments_) {
  return [
    'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
    `data: ${JSON.stringify({
      choices: [{
        delta: {
          tool_calls: [{
            index: 0,
            id: callId,
            type: "function",
            function: { name, arguments: JSON.stringify(arguments_) },
          }],
        },
      }],
    })}`,
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":3}}',
    "data: [DONE]",
    "",
  ].join("\n\n");
}


function textStream(text) {
  return [
    'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
    `data: ${JSON.stringify({
      choices: [{
        delta: { content: text },
        finish_reason: "stop",
      }],
      usage: { prompt_tokens: 6, completion_tokens: 2 },
    })}`,
    "data: [DONE]",
    "",
  ].join("\n\n");
}


async function localDeepSeekScript(responses) {
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      const index = requests.length;
      requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      const body = responses[index];
      assert.notEqual(body, undefined);
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "content-length": Buffer.byteLength(body),
      });
      response.end(body);
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise(resolve => server.close(resolve)),
  };
}


function nativeSessionInput(baseUrl, overrides = {}) {
  return {
    materialized: {
      promptSections: [],
      contextItems: [],
      messages: [{ message_id: "message-current", role: "user", content: "Use the tool" }],
    },
    provider: {
      model: "deepseek-native-tools",
      route: {
        protocol: "deepseek",
        base_url: baseUrl,
        credential_mode: "reference",
        metadata_headers: [],
        thinking_enabled: false,
        reasoning_effort: "off",
      },
    },
    credential: () => "test-only-credential",
    sessionId: IDENTITY.session_id,
    signal: new AbortController().signal,
    toolManifest: [manifestEntry()],
    runIdentity: IDENTITY,
    ...overrides,
  };
}


test("native ToolRuntime delegates a frozen manifest tool to the identity-bound platform callback", async () => {
  const calls = [];
  const manifest = [{
    tool_id: "platform_lookup",
    schema: {
      type: "object",
      properties: { query: { type: "string" } },
      required: ["query"],
      additionalProperties: false,
    },
    version: "3",
    read_only: true,
    timeout_ms: 250,
    idempotency: "idempotent",
  }];
  const ctx = await nativeTools({
    toolManifest: manifest,
    runIdentity: IDENTITY,
    executeTool: async call => {
      calls.push(call);
      assert.equal(Object.isFrozen(call), true);
      assert.equal(Object.isFrozen(call.identity), true);
      assert.equal(Object.isFrozen(call.manifest), true);
      assert.equal(call.signal.aborted, false);
      return { status: "completed", output: { answer: "platform-result" } };
    },
  });

  try {
    assert.deepEqual(ctx.tools.schemas(), [{
      name: "platform_lookup",
      description: "Platform tool platform_lookup (version 3)",
      parameters: manifest[0].schema,
    }]);
    const result = await ctx.tools.execute({
      callId: "tool-call-native-1",
      name: "platform_lookup",
      arguments: { query: "bounded" },
      signal: new AbortController().signal,
    });

    assert.deepEqual(calls.map(call => ({
      tool_call_id: call.tool_call_id,
      tool_id: call.tool_id,
      arguments: call.arguments,
      identity: call.identity,
      manifest: call.manifest,
    })), [{
      tool_call_id: "tool-call-native-1",
      tool_id: "platform_lookup",
      arguments: { query: "bounded" },
      identity: IDENTITY,
      manifest: manifest[0],
    }]);
    assert.equal(result.isError, false);
    assert.deepEqual(result.value, { output: { answer: "platform-result" } });
    assert.deepEqual(result.content, [{
      type: "text",
      text: '{"output":{"answer":"platform-result"}}',
    }]);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("invalid native manifest registration leaves no partially registered platform tools", async () => {
  const ctx = await toolContext();
  const toolManifest = [
    {
      tool_id: "valid_first",
      schema: { type: "object" },
      version: "1",
      read_only: true,
      timeout_ms: 100,
      idempotency: "idempotent",
    },
    {
      tool_id: "invalid_second",
      schema: null,
      version: "1",
      read_only: true,
      timeout_ms: 100,
      idempotency: "idempotent",
    },
  ];

  try {
    assert.throws(() => registerPlatformTools(ctx, {
      toolManifest,
      runIdentity: IDENTITY,
      executeTool: async () => ({ status: "completed", output: null }),
    }));
    assert.deepEqual(ctx.tools.schemas(), []);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("second native registration failure rolls back the first registered platform tool", async () => {
  const ctx = await toolContext();
  try {
    assert.throws(() => registerPlatformTools(ctx, {
      toolManifest: [
        manifestEntry({ tool_id: "registered_before_failure" }),
        manifestEntry({ tool_id: "run_code" }),
      ],
      runIdentity: IDENTITY,
      executeTool: async () => ({ status: "completed", output: null }),
    }), /reserved/);
    assert.deepEqual(ctx.tools.schemas(), []);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("native adapter rejects duplicate ids, unsupported schemas, and invalid timeouts", async () => {
  const cases = [
    [manifestEntry(), manifestEntry()],
    [manifestEntry({ schema: { type: "unsupported-type" } })],
    [manifestEntry({ timeout_ms: 0 })],
  ];

  for (const toolManifest of cases) {
    const ctx = await toolContext();
    try {
      assert.throws(() => registerPlatformTools(ctx, {
        toolManifest,
        runIdentity: IDENTITY,
        executeTool: async () => ({ status: "completed", output: null }),
      }));
      assert.deepEqual(ctx.tools.schemas(), []);
    } finally {
      await ctx.fiber.dispose();
    }
  }
});


test("native adapter accepts the Node timer boundary and rejects overflow", async () => {
  const maximumTimerDelayMs = 2_147_483_647;
  const accepted = await toolContext();
  try {
    registerPlatformTools(accepted, {
      toolManifest: [manifestEntry({ timeout_ms: maximumTimerDelayMs })],
      runIdentity: IDENTITY,
      executeTool: async () => ({ status: "completed", output: null }),
    });
    assert.deepEqual(accepted.tools.schemas().map(tool => tool.name), ["platform_lookup"]);
  } finally {
    await accepted.fiber.dispose();
  }

  const rejected = await toolContext();
  try {
    assert.throws(() => registerPlatformTools(rejected, {
      toolManifest: [manifestEntry({ timeout_ms: maximumTimerDelayMs + 1 })],
      runIdentity: IDENTITY,
      executeTool: async () => ({ status: "completed", output: null }),
    }), /timeout/);
    assert.deepEqual(rejected.tools.schemas(), []);
  } finally {
    await rejected.fiber.dispose();
  }
});


test("native adapter rejects an incomplete platform run identity", async () => {
  const ctx = await toolContext();
  const { step_id: _missing, ...incompleteIdentity } = IDENTITY;
  try {
    assert.throws(() => registerPlatformTools(ctx, {
      toolManifest: [manifestEntry()],
      runIdentity: incompleteIdentity,
      executeTool: async () => ({ status: "completed", output: null }),
    }), /identity/);
    assert.deepEqual(ctx.tools.schemas(), []);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("native Session records the platform call and completed result in one lifecycle", async () => {
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      const first = requests.length === 1;
      const body = first ? [
        'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"tool-call-session-1","type":"function","function":{"name":"platform_lookup","arguments":"{\\"query\\":\\"native\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":3}}',
        "data: [DONE]",
        "",
      ].join("\n\n") : [
        'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
        'data: {"choices":[{"delta":{"content":"tool complete"},"finish_reason":"stop"}],"usage":{"prompt_tokens":6,"completion_tokens":2}}',
        "data: [DONE]",
        "",
      ].join("\n\n");
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "content-length": Buffer.byteLength(body),
      });
      response.end(body);
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const calls = [];

  try {
    const result = await runDeepSeekHarnessSession({
      materialized: {
        promptSections: [],
        contextItems: [],
        messages: [{ message_id: "message-current", role: "user", content: "Use the tool" }],
      },
      provider: {
        model: "deepseek-native-tools",
        route: {
          protocol: "deepseek",
          base_url: `http://127.0.0.1:${address.port}`,
          credential_mode: "reference",
          metadata_headers: [],
          thinking_enabled: false,
          reasoning_effort: "off",
        },
      },
      credential: () => "test-only-credential",
      sessionId: IDENTITY.session_id,
      signal: new AbortController().signal,
      toolManifest: [{
        tool_id: "platform_lookup",
        schema: {
          type: "object",
          properties: { query: { type: "string" } },
          required: ["query"],
          additionalProperties: false,
        },
        version: "3",
        read_only: true,
        timeout_ms: 250,
        idempotency: "idempotent",
      }],
      runIdentity: IDENTITY,
      executeTool: async call => {
        calls.push(call);
        return { status: "completed", output: { answer: "from-platform" } };
      },
    });

    assert.equal(result.content, "tool complete");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].tool_call_id, "tool-call-session-1");
    const callEvent = result.nativeEvents.find(event => event.type === "tool/call");
    const resultEvent = result.nativeEvents.find(event => event.type === "tool/result");
    assert.equal(callEvent.data.callId, "tool-call-session-1");
    assert.equal(resultEvent.data.message.source.callId, "tool-call-session-1");
    assert.equal(resultEvent.data.message.content[0].toolCallId, "tool-call-session-1");
    assert.equal(resultEvent.data.message.content[0].isError, false);
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].tools.map(tool => tool.function.name), ["platform_lookup"]);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});


test("native Session keeps a failed platform callback correlated through tool/result and turn/end", async () => {
  const upstream = await localDeepSeekScript([
    toolCallStream("tool-call-session-failed", "platform_lookup", { query: "native" }),
    textStream("platform tool failed; no result claimed"),
  ]);

  try {
    const result = await runDeepSeekHarnessSession(nativeSessionInput(upstream.baseUrl, {
      executeTool: async () => ({
        status: "failed",
        output: { message: "platform rejected the call" },
      }),
    }));
    const callIndex = result.nativeEvents.findIndex(event => event.type === "tool/call");
    const resultIndex = result.nativeEvents.findIndex(event => event.type === "tool/result");
    const terminalIndex = result.nativeEvents.findLastIndex(event => event.type === "turn/end");
    const callEvent = result.nativeEvents[callIndex];
    const resultEvent = result.nativeEvents[resultIndex];
    const terminalEvent = result.nativeEvents[terminalIndex];

    assert.equal(callEvent.data.callId, "tool-call-session-failed");
    assert.equal(resultEvent.data.message.source.callId, "tool-call-session-failed");
    assert.equal(resultEvent.data.message.content[0].toolCallId, "tool-call-session-failed");
    assert.equal(resultEvent.data.message.content[0].isError, true);
    assert.match(resultEvent.data.message.content[0].content[0].text, /platform rejected/);
    assert.ok(
      callIndex < resultIndex && resultIndex < terminalIndex,
      JSON.stringify(result.nativeEvents.map(event => event.type)),
    );
    assert.equal(terminalEvent.data.reason.kind, "completed");
    assert.equal(result.content, "platform tool failed; no result claimed");
    assert.equal(upstream.requests.length, 2);
  } finally {
    await upstream.close();
  }
});


test("native Session cancellation aborts the platform callback before an aborted turn/end", async () => {
  const upstream = await localDeepSeekScript([
    toolCallStream("tool-call-session-cancelled", "platform_lookup", { query: "native" }),
  ]);
  const controller = new AbortController();
  const nativeEvents = [];
  let releaseStarted;
  const started = new Promise(resolve => { releaseStarted = resolve; });
  let callbackSignal;

  try {
    const completion = runDeepSeekHarnessSession(nativeSessionInput(upstream.baseUrl, {
      signal: controller.signal,
      onEvent: event => nativeEvents.push(event),
      executeTool: async call => {
        callbackSignal = call.signal;
        releaseStarted();
        await new Promise(resolve => {
          if (call.signal.aborted) resolve();
          else call.signal.addEventListener("abort", resolve, { once: true });
        });
        return { status: "completed", output: { stale: true } };
      },
    }));
    await started;
    controller.abort(new Error("cancelled by host"));
    await assert.rejects(completion, error => error instanceof DshSessionFailure);

    const callIndex = nativeEvents.findIndex(event => event.type === "tool/call");
    const resultIndex = nativeEvents.findIndex(event => event.type === "tool/result");
    const terminalIndex = nativeEvents.findLastIndex(event => event.type === "turn/end");
    const resultEvent = nativeEvents[resultIndex];
    const terminalEvent = nativeEvents[terminalIndex];
    assert.equal(callbackSignal.aborted, true);
    assert.equal(nativeEvents[callIndex].data.callId, "tool-call-session-cancelled");
    assert.equal(resultEvent.data.message.source.callId, "tool-call-session-cancelled");
    assert.equal(resultEvent.data.message.content[0].isError, true);
    assert.ok(
      callIndex < resultIndex && resultIndex < terminalIndex,
      JSON.stringify(nativeEvents.map(event => event.type)),
    );
    assert.equal(terminalEvent.data.reason.kind, "aborted");
    assert.equal(upstream.requests.length, 1);
  } finally {
    await upstream.close();
  }
});


test("platform failed status becomes a native ToolRuntime error result", async () => {
  const ctx = await nativeTools({
    toolManifest: [manifestEntry()],
    runIdentity: IDENTITY,
    executeTool: async () => ({
      status: "failed",
      output: { message: "platform rejected the call" },
    }),
  });

  try {
    const result = await ctx.tools.execute({
      callId: "tool-call-failed",
      name: "platform_lookup",
      arguments: {},
      signal: new AbortController().signal,
    });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /platform rejected the call/);
    assert.equal("value" in result, false);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("native ToolRuntime rejects an unconfirmed completed write result", async () => {
  const ctx = await nativeTools({
    toolManifest: [manifestEntry({
      tool_id: "platform_write",
      read_only: false,
      idempotency: "non_idempotent",
    })],
    runIdentity: IDENTITY,
    executeTool: async () => ({ status: "completed", output: { changed: true } }),
  });

  try {
    const result = await ctx.tools.execute({
      callId: "tool-call-write-unconfirmed",
      name: "platform_write",
      arguments: {},
      signal: new AbortController().signal,
    });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /unconfirmed.*effect_id/);
    assert.equal("value" in result, false);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("native ToolRuntime returns only a platform-confirmed write effect_id", async () => {
  const ctx = await nativeTools({
    toolManifest: [manifestEntry({
      tool_id: "platform_write",
      read_only: false,
      idempotency: "non_idempotent",
    })],
    runIdentity: IDENTITY,
    executeTool: async () => ({
      status: "completed",
      output: { changed: true },
      effect_id: "effect-from-platform",
    }),
  });

  try {
    const result = await ctx.tools.execute({
      callId: "tool-call-write-confirmed",
      name: "platform_write",
      arguments: {},
      signal: new AbortController().signal,
    });
    assert.equal(result.isError, false);
    assert.deepEqual(result.value, {
      output: { changed: true },
      effect_id: "effect-from-platform",
    });
  } finally {
    await ctx.fiber.dispose();
  }
});


test("native ToolRuntime forwards caller cancellation to the active platform callback", async () => {
  const controller = new AbortController();
  let releaseStarted;
  const started = new Promise(resolve => { releaseStarted = resolve; });
  let callbackSignal;
  const ctx = await nativeTools({
    toolManifest: [manifestEntry()],
    runIdentity: IDENTITY,
    executeTool: async call => {
      callbackSignal = call.signal;
      releaseStarted();
      await new Promise(resolve => {
        if (call.signal.aborted) resolve();
        else call.signal.addEventListener("abort", resolve, { once: true });
      });
      return { status: "completed", output: { stale: true } };
    },
  });

  try {
    const executing = ctx.tools.execute({
      callId: "tool-call-cancelled",
      name: "platform_lookup",
      arguments: {},
      signal: controller.signal,
    });
    await started;
    controller.abort(new Error("cancelled by host"));
    const result = await executing;
    assert.equal(callbackSignal.aborted, true);
    assert.equal(result.isError, true);
    assert.equal("value" in result, false);
  } finally {
    await ctx.fiber.dispose();
  }
});


test("native ToolRuntime timeout aborts the platform callback and cannot become success", async () => {
  let callbackSignal;
  const ctx = await nativeTools({
    toolManifest: [manifestEntry({ timeout_ms: 10 })],
    runIdentity: IDENTITY,
    executeTool: async call => {
      callbackSignal = call.signal;
      await new Promise(resolve => {
        if (call.signal.aborted) resolve();
        else call.signal.addEventListener("abort", resolve, { once: true });
      });
      return { status: "completed", output: { late: true } };
    },
  });

  try {
    const result = await ctx.tools.execute({
      callId: "tool-call-timeout",
      name: "platform_lookup",
      arguments: {},
      signal: new AbortController().signal,
    });
    assert.equal(callbackSignal.aborted, true);
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /timed out/);
    assert.equal("value" in result, false);
  } finally {
    await ctx.fiber.dispose();
  }
});

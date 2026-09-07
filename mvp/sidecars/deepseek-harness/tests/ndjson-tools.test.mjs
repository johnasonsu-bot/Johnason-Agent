import assert from "node:assert/strict";
import { createServer } from "node:http";
import { PassThrough } from "node:stream";
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { EphemeralGrantChannel } from "../src/grant-channel.ts";
import { createSidecar, serveNdjson } from "../src/server.ts";


const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const QUERY_FIXTURE = JSON.parse(readFileSync(
  path.join(ROOT, "tests/runtime-query-v2-fixture.json"),
  "utf8",
));
const PROVIDER_REF = "provider-profile:deepseek-ndjson-tools";


function toolManifest() {
  return [{
    tool_id: "platform_lookup",
    schema: {
      type: "object",
      properties: { query: { type: "string" } },
      required: ["query"],
      additionalProperties: false,
    },
    version: "3",
    read_only: true,
    timeout_ms: 1_000,
    idempotency: "idempotent",
  }];
}


function toolCallStream(callId = "tool-call-wire-1") {
  return [
    'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
    `data: ${JSON.stringify({ choices: [{ delta: { tool_calls: [{
      index: 0,
      id: callId,
      type: "function",
      function: { name: "platform_lookup", arguments: '{"query":"native"}' },
    }] } }] })}`,
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":4,"completion_tokens":3}}',
    "data: [DONE]",
    "",
  ].join("\n\n");
}


function textStream(text) {
  return [
    'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}',
    `data: ${JSON.stringify({ choices: [{ delta: { content: text }, finish_reason: "stop" }], usage: { prompt_tokens: 6, completion_tokens: 2 } })}`,
    "data: [DONE]",
    "",
  ].join("\n\n");
}


async function localDeepSeek(responses) {
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      const body = responses[requests.length];
      requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
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


function queryCommand() {
  const command = structuredClone(QUERY_FIXTURE);
  command.payload.envelope.provider_ref = PROVIDER_REF;
  command.payload.envelope.runtime.build_id = "dsh:model-host-v2-r1";
  command.payload.envelope.tool_manifest = toolManifest();
  return command;
}


function sourceWire(sidecar, grantReady = Promise.resolve()) {
  const input = new PassThrough();
  const output = new PassThrough();
  const lines = createInterface({ input: output, crlfDelay: Infinity });
  const frames = [];
  const waiters = [];
  lines.on("line", line => {
    const frame = JSON.parse(line);
    const waiter = waiters.shift();
    if (waiter) waiter.resolve(frame);
    else frames.push(frame);
  });
  const serverLines = serveNdjson(sidecar, input, output, grantReady);
  const endInput = async () => {
    if (!input.writableEnded) {
      const closed = new Promise(resolve => serverLines.once("close", resolve));
      input.end();
      await closed;
    }
    await sidecar.shutdown();
  };
  return {
    send(command) {
      input.write(`${JSON.stringify({ kind: "command", ...command })}\n`);
    },
    read(timeoutMs = 1_000) {
      if (frames.length !== 0) return Promise.resolve(frames.shift());
      return new Promise((resolve, reject) => {
        const waiter = {
          resolve(value) {
            clearTimeout(timer);
            resolve(value);
          },
        };
        const timer = setTimeout(() => {
          const index = waiters.indexOf(waiter);
          if (index !== -1) waiters.splice(index, 1);
          reject(new Error("timed out waiting for NDJSON frame"));
        }, timeoutMs);
        waiters.push(waiter);
      });
    },
    async expectQuiet(durationMs = 30) {
      assert.equal(frames.length, 0, JSON.stringify(frames));
      await new Promise((resolve, reject) => {
        const onLine = line => {
          clearTimeout(timer);
          reject(new Error(`unexpected NDJSON frame: ${line}`));
        };
        const timer = setTimeout(() => {
          lines.off("line", onLine);
          resolve();
        }, durationMs);
        lines.once("line", onLine);
      });
    },
    endInput,
    async close() {
      await endInput();
      lines.close();
      output.destroy();
    },
  };
}


function acceptGrant(grants, baseUrl) {
  grants.accept({
    grant_id: "grant-ndjson-tools",
    grant_digest: "a".repeat(64),
    target_instance_digest: "b".repeat(64),
    command_id: "start-1",
    run_id: "run-wire",
    term_id: "term-wire",
    step_id: "step-wire",
    provider_ref: PROVIDER_REF,
    route: {
      protocol: "deepseek",
      base_url: baseUrl,
      credential_mode: "reference",
      metadata_headers: [],
      thinking_enabled: false,
      reasoning_effort: "high",
    },
    model: "deepseek-native-tools",
    expires_at: 110,
  }, Buffer.from("test-only-credential", "utf8"));
  return grants;
}


function grantChannel(baseUrl) {
  return acceptGrant(new EphemeralGrantChannel(() => 100), baseUrl);
}


async function harness(responses) {
  const upstream = await localDeepSeek(responses);
  const sidecar = createSidecar({
    grantChannel: grantChannel(upstream.baseUrl),
    runtimeId: "dsh",
    buildId: "dsh:model-host-v2-r1",
    instanceDigest: "b".repeat(64),
  });
  return { upstream, wire: sourceWire(sidecar), sidecar };
}


async function startToolQuery(wire, command = queryCommand()) {
  wire.send(command);
  const accepted = await wire.read();
  const running = await wire.read();
  const execute = await wire.read();
  assert.deepEqual(accepted.payload, { accepted: true });
  assert.deepEqual(running.payload.payload, { status: "running" });
  assert.equal(running.payload.cursor, 1);
  assert.equal(execute.kind, "request");
  assert.equal(execute.type, "tool.execute");
  assert.deepEqual(execute.payload, {
    identity: {
      session_id: "session-1",
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      command_id: "start-1",
      agent_id: "agent-1",
      agent_role: "worker",
    },
    tool_call_id: "tool-call-wire-1",
    tool_id: "platform_lookup",
    arguments: { query: "native" },
  });
  return execute;
}


function toolResult(execute, overrides = {}) {
  return {
    type: "tool.result",
    command_id: execute.request_id,
    payload: {
      identity: structuredClone(execute.payload.identity),
      tool_call_id: execute.payload.tool_call_id,
      tool_id: execute.payload.tool_id,
      status: "completed",
      output: { answer: "from-platform" },
      effect_id: null,
      ...overrides,
    },
  };
}


async function readCompleted(wire) {
  const delta = await wire.read();
  const message = await wire.read();
  const terminal = await wire.read();
  assert.equal(delta.payload.type, "assistant.delta");
  assert.equal(message.payload.type, "assistant.message");
  assert.deepEqual(terminal.payload.payload, { status: "completed" });
  return terminal.payload;
}


test("production source NDJSON delegates a native tool and accepts one correlated result", async () => {
  const { upstream, wire } = await harness([
    toolCallStream(),
    textStream("tool complete"),
  ]);
  try {
    const execute = await startToolQuery(wire);
    wire.send(toolResult(execute));
    const terminal = await readCompleted(wire);
    assert.equal(terminal.cursor, 4);
    assert.equal(upstream.requests.length, 2);
    assert.deepEqual(upstream.requests[0].tools.map(tool => tool.function.name), [
      "platform_lookup",
    ]);
    wire.send({
      type: "query.status",
      command_id: "seal-tool-query",
      payload: {
        run_id: "run-wire",
        term_id: "term-wire",
        step_id: "step-wire",
        terminal_cursor: 4,
      },
    });
    assert.equal((await wire.read()).payload.sealed, true);
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("production source NDJSON remains compatible with an empty tool manifest", async () => {
  const { upstream, wire } = await harness([textStream("no platform tools")]);
  try {
    const command = queryCommand();
    command.payload.envelope.tool_manifest = [];
    wire.send(command);
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    assert.deepEqual((await wire.read()).payload.payload, { status: "running" });
    const terminal = await readCompleted(wire);
    assert.equal(terminal.cursor, 4);
    assert.equal(upstream.requests.length, 1);
    assert.equal("tools" in upstream.requests[0], false);
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("a correlated platform failure reaches native tool result without a false sidecar failure", async () => {
  const { upstream, wire } = await harness([
    toolCallStream(),
    textStream("tool failure handled"),
  ]);
  try {
    const execute = await startToolQuery(wire);
    wire.send(toolResult(execute, {
      status: "failed",
      output: { message: "platform rejected the call" },
    }));
    const terminal = await readCompleted(wire);
    assert.equal(terminal.cursor, 4);
    assert.equal(upstream.requests.length, 2);
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("wrong identity, ids, and duplicate tool results are rejected without acknowledgement", async () => {
  const { upstream, wire } = await harness([
    toolCallStream(),
    textStream("correct result handled"),
  ]);
  try {
    const execute = await startToolQuery(wire);
    wire.send(toolResult(execute, {
      identity: { ...execute.payload.identity, agent_id: "wrong-agent" },
    }));
    await wire.expectQuiet();
    wire.send({ ...toolResult(execute), command_id: "wrong-request-id" });
    await wire.expectQuiet();
    wire.send(toolResult(execute, { tool_call_id: "wrong-tool-call" }));
    await wire.expectQuiet();
    wire.send(toolResult(execute, { tool_id: "wrong-tool" }));
    await wire.expectQuiet();

    const correct = toolResult(execute);
    wire.send(correct);
    await readCompleted(wire);
    wire.send(correct);
    await wire.expectQuiet();
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("query cancellation waits for platform executor settlement before terminal", async () => {
  const { upstream, wire } = await harness([toolCallStream()]);
  try {
    const execute = await startToolQuery(wire);
    wire.send({
      type: "query.cancel",
      command_id: "cancel-tool-query",
      payload: { run_id: "run-wire", reason: "user_requested" },
    });
    const cancel = await wire.read();
    assert.deepEqual(cancel, {
      kind: "request",
      type: "tool.cancel",
      request_id: execute.request_id,
      payload: {
        identity: execute.payload.identity,
        tool_call_id: "tool-call-wire-1",
        tool_id: "platform_lookup",
      },
    });
    await wire.expectQuiet();

    wire.send(toolResult(execute, {
      status: "failed",
      output: { message: "cancelled by platform" },
    }));
    const response = await wire.read();
    const terminal = await wire.read();
    assert.equal(response.kind, "response");
    assert.equal(response.command_id, "cancel-tool-query");
    assert.deepEqual(response.payload, { accepted: true });
    assert.equal(terminal.kind, "event");
    assert.deepEqual(terminal.payload.payload, { status: "cancelled" });
    assert.equal(terminal.payload.cursor, 2);
    assert.equal(upstream.requests.length, 1);
    wire.send({
      type: "query.status",
      command_id: "seal-cancelled-tool-query",
      payload: {
        run_id: "run-wire",
        term_id: "term-wire",
        step_id: "step-wire",
        terminal_cursor: 2,
      },
    });
    assert.equal((await wire.read()).payload.sealed, true);
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("query cancellation accepted during native setup never starts a provider request", async () => {
  const { upstream, wire } = await harness([textStream("must not be requested")]);
  try {
    const command = queryCommand();
    command.payload.envelope.tool_manifest = [];
    wire.send(command);
    wire.send({
      type: "query.cancel",
      command_id: "cancel-during-native-setup",
      payload: { run_id: "run-wire", reason: "user_requested" },
    });
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    assert.deepEqual((await wire.read()).payload.payload, { status: "running" });
    const response = await wire.read();
    const terminal = await wire.read();
    assert.equal(response.command_id, "cancel-during-native-setup");
    assert.deepEqual(response.payload, { accepted: true });
    assert.deepEqual(terminal.payload.payload, { status: "cancelled" });
    assert.equal(upstream.requests.length, 0);
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("EOF closes a pending tool executor without a fabricated terminal seal", async () => {
  const { upstream, wire, sidecar } = await harness([toolCallStream()]);
  try {
    const command = queryCommand();
    command.payload.envelope.tool_manifest[0].timeout_ms = 25;
    await startToolQuery(wire, command);
    await wire.endInput();
    await wire.expectQuiet(60);
    assert.throws(() => sidecar.seal({
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 2,
    }), /terminal/);
    assert.equal(upstream.requests.length, 1);
  } finally {
    await wire.close();
    await upstream.close();
  }
});


test("EOF permanently fences a query waiting for grant readiness", async () => {
  const upstream = await localDeepSeek([textStream("must not start after EOF")]);
  const grants = new EphemeralGrantChannel(() => 100);
  const sidecar = createSidecar({
    grantChannel: grants,
    runtimeId: "dsh",
    buildId: "dsh:model-host-v2-r1",
    instanceDigest: "b".repeat(64),
  });
  let releaseReady;
  const ready = new Promise(resolve => { releaseReady = resolve; });
  let markWaiting;
  const waiting = new Promise(resolve => { markWaiting = resolve; });
  const trackedReady = {
    then(resolve, reject) {
      markWaiting();
      return ready.then(resolve, reject);
    },
  };
  const wire = sourceWire(sidecar, trackedReady);
  try {
    const command = queryCommand();
    command.payload.envelope.tool_manifest = [];
    wire.send(command);
    await waiting;
    await wire.endInput();
    acceptGrant(grants, upstream.baseUrl);
    assert.throws(() => sidecar.startQuery(command.payload), /shut down/);
    releaseReady();
    await new Promise(resolve => setImmediate(resolve));
    await wire.expectQuiet(60);
    assert.equal(upstream.requests.length, 0);
    assert.throws(() => sidecar.seal({
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 1,
    }), /terminal/);
  } finally {
    releaseReady();
    await wire.close();
    await upstream.close();
  }
});

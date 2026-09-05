import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import path from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadFixedPreset } from "../src/bootstrap.ts";
import { createCheckpoint, sealAcknowledgement } from "../src/checkpoint.ts";
import { mapSessionEvents, sortPromptSections } from "../src/event-mapper.ts";
import { EphemeralGrantChannel } from "../src/grant-channel.ts";
import { runDeepSeekHarnessSession } from "../src/native-session.ts";
import { createSidecar } from "../src/server.ts";


const FIXED_PLUGINS = [
  "@deepseek-ai/dsh-agent",
  "@deepseek-ai/dsh-session-persistence-jsonl",
  "@deepseek-ai/dsh-session-checkpoint-policy",
  "@deepseek-ai/dsh-llm-deepseek",
  "@johnason/deepseek-harness-host-v2",
];

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ENTRYPOINT = path.join(ROOT, "dist/deepseek-harness-host-v2.mjs");
const INSTANCE_DIGEST = "b".repeat(64);
const QUERY_FIXTURE = JSON.parse(readFileSync(
  path.join(ROOT, "tests/runtime-query-v2-fixture.json"),
  "utf8",
));


function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}


function digest(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}


function queryCommand(providerRef = "provider-profile:fixture-completed") {
  const command = structuredClone(QUERY_FIXTURE);
  command.payload.envelope.provider_ref = providerRef;
  return command;
}


function privateGrant(
  providerRef = "provider-profile:fixture-completed",
  grantId = "grant-wire",
  instanceDigest = INSTANCE_DIGEST,
) {
  const { envelope } = queryCommand(providerRef).payload;
  const issuedAt = Date.now() / 1000;
  const binding = {
    grant_id: grantId,
    target: {
      runtime_id: "dsh",
      build_id: "dsh:fixed-host-v2-smoke",
      lease_id: "lease-wire",
      instance_id_digest: instanceDigest,
      instance_nonce_digest: "c".repeat(64),
      host_generation: "host-a",
      lease_generation_seq: 1,
      expires_at: issuedAt + 60,
    },
    session_id: envelope.session_id,
    command_id: envelope.command_id,
    run_id: envelope.run_id,
    term_id: envelope.term_id,
    step_id: envelope.step_id,
    provider_id: providerRef.replace("provider-profile:", ""),
    provider_profile_digest: "d".repeat(64),
    route: {
      protocol: "deepseek",
      base_url: "https://api.deepseek.com",
      credential_mode: "reference",
      metadata_headers: [],
      thinking_enabled: true,
      reasoning_effort: "high",
    },
    model: "fixture-model-resolved",
    scopes: ["inference"],
    issued_at: issuedAt,
    expires_at: issuedAt + 30,
    grant_nonce_digest: "e".repeat(64),
  };
  return {
    schema: "workbench.runtime.provider_grant_private.v1",
    binding,
    grant_digest: digest(binding),
    secret: Buffer.from([1, 2, 3, 4]),
  };
}


function providerRoute() {
  return {
    protocol: "deepseek",
    base_url: "https://api.deepseek.com",
    credential_mode: "reference",
    metadata_headers: [],
    thinking_enabled: true,
    reasoning_effort: "high",
  };
}


function encodePrivateGrant(grant) {
  const header = Buffer.from(JSON.stringify({
    schema: grant.schema,
    binding: grant.binding,
    grant_digest: grant.grant_digest,
  }), "utf8");
  const prefix = Buffer.alloc(17);
  prefix.write("JAGTGRN1", 0, "ascii");
  prefix.writeUInt8(1, 8);
  prefix.writeUInt32BE(header.length, 9);
  prefix.writeUInt32BE(grant.secret.length, 13);
  return Buffer.concat([prefix, header, grant.secret]);
}


async function readPrivateAck(stream) {
  let buffered = Buffer.alloc(0);
  for await (const chunk of stream) {
    buffered = Buffer.concat([buffered, chunk]);
    if (buffered.length < 13) continue;
    assert.equal(buffered.subarray(0, 8).toString("ascii"), "JAGTACK1");
    assert.equal(buffered.readUInt8(8), 1);
    const size = buffered.readUInt32BE(9);
    if (buffered.length < 13 + size) continue;
    return JSON.parse(buffered.subarray(13, 13 + size).toString("utf8"));
  }
  throw new Error("private grant acknowledgement was not received");
}


async function launchPublishedSidecar(grant = privateGrant()) {
  const child = spawn(process.execPath, [ENTRYPOINT], {
    cwd: ROOT,
    env: {
      PATH: process.env.PATH ?? "",
      WORKBENCH_PROVIDER_GRANT_FD: "3",
    },
    stdio: ["pipe", "pipe", "pipe", "pipe"],
  });
  const ackPromise = readPrivateAck(child.stdio[3]);
  child.stdio[3].write(encodePrivateGrant(grant));
  const ack = await ackPromise;
  assert.deepEqual(ack, {
    schema: "workbench.runtime.provider_grant_ack.v1",
    grant_id: grant.binding.grant_id,
    grant_digest: grant.grant_digest,
    target_instance_digest: grant.binding.target.instance_id_digest,
  });
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  const iterator = lines[Symbol.asyncIterator]();
  const stderr = [];
  child.stderr.on("data", chunk => stderr.push(chunk));
  return {
    child,
    send(command) {
      child.stdin.write(`${JSON.stringify({ kind: "command", ...command })}\n`);
    },
    async read() {
      const next = await iterator.next();
      assert.equal(next.done, false, Buffer.concat(stderr).toString("utf8"));
      return JSON.parse(next.value);
    },
    async close() {
      child.stdin.end();
      const [code] = await once(child, "exit");
      assert.equal(code, 0, Buffer.concat(stderr).toString("utf8"));
      lines.close();
    },
  };
}


test("build receipt binds actual artifacts to the exact sidecar source", () => {
  const receipt = JSON.parse(readFileSync(path.join(ROOT, "dist/build-receipt.json"), "utf8"));

  assert.equal(receipt.schema, "workbench.runtime.dsh.host_v2_build_receipt.v1");
  assert.equal(receipt.command, "npm run build");
  assert.match(receipt.source_digest, /^[0-9a-f]{64}$/);
  assert.match(receipt.artifact_digest, /^[0-9a-f]{64}$/);
  assert.equal(receipt.artifacts.length, 7);
});


test("fixed preset rejects dynamic plugin policy", () => {
  assert.throws(
    () => loadFixedPreset({
      schema: "workbench.runtime.dsh.fixed_preset.v1",
      runtime_id: "dsh",
      plugins: FIXED_PLUGINS,
      policy: { plugin_download: true, user_plugin_scan: false },
    }),
    /dynamic plugin/,
  );
});


test("prompt sections sort stably by order then section id", () => {
  const sorted = sortPromptSections([
    { order: 20, section_id: "tools", content: "T" },
    { order: 10, section_id: "zeta", content: "Z" },
    { order: 10, section_id: "alpha", content: "A" },
  ]);

  assert.deepEqual(sorted.map(({ order, section_id }) => [order, section_id]), [
    [10, "alpha"],
    [10, "zeta"],
    [20, "tools"],
  ]);
});


test("prompt section id ordering is locale independent", () => {
  const sorted = sortPromptSections([
    { order: 10, section_id: "ä", content: "second" },
    { order: 10, section_id: "z", content: "first" },
  ]);

  assert.deepEqual(sorted.map(section => section.section_id), ["z", "ä"]);
});


test("DSH sequence maps to monotonic Host cursor and durable terminal", () => {
  const mapped = mapSessionEvents({
    run_id: "run-1",
    term_id: "term-1",
    step_id: "step-1",
  }, [
    { seq: 0, type: "assistant/delta", data: { content: "hello" } },
    { seq: 1, type: "assistant/message", data: { content: "hello" } },
    { seq: 2, type: "turn/end", data: { reason: "completed" } },
  ]);

  assert.deepEqual(mapped.map(event => event.cursor), [1, 2, 3]);
  assert.equal(mapped.at(-1).type, "runtime.status");
  assert.deepEqual(mapped.at(-1).payload, { status: "completed" });
});


test("failed and cancelled DSH turns keep distinct terminal semantics", () => {
  for (const [reason, status, terminalEvent] of [
    ["failed", "failed", "query.failed"],
    ["cancelled", "cancelled", "query.cancelled"],
  ]) {
    const [event] = mapSessionEvents({
      run_id: `run-${reason}`,
      term_id: `term-${reason}`,
      step_id: `step-${reason}`,
    }, [{ seq: 0, type: "turn/end", data: { reason } }]);
    assert.equal(event.type, "runtime.status");
    assert.deepEqual(event.payload, { status });
    assert.equal(event.payload.status, status);
  }
});


test("event mapper rejects a DSH sequence gap", () => {
  assert.throws(() => mapSessionEvents({
    run_id: "run-gap",
    term_id: "term-gap",
    step_id: "step-gap",
  }, [
    { seq: 0, type: "assistant/delta", data: { content: "a" } },
    { seq: 2, type: "turn/end", data: { reason: "completed" } },
  ]), /sequence gap/);
});


test("grant channel consumes once and wipes the transient secret", () => {
  const grants = new EphemeralGrantChannel(() => 100);
  const secret = Buffer.from("fixture-secret", "utf8");
  const ack = grants.accept({
    grant_id: "grant-1",
    grant_digest: "a".repeat(64),
    target_instance_digest: "b".repeat(64),
    command_id: "command-1",
    run_id: "run-1",
    term_id: "term-1",
    step_id: "step-1",
    provider_ref: "provider-1",
    route: providerRoute(),
    model: "model-1",
    expires_at: 110,
  }, secret);

  assert.equal(ack.grant_id, "grant-1");
  const envelope = {
    command_id: "command-1", run_id: "run-1", term_id: "term-1", step_id: "step-1",
    provider_ref: "provider-1", model: "model-1",
  };
  const consumed = grants.consumeForEnvelope(envelope);
  assert.equal(consumed.secret.toString("utf8"), "fixture-secret");
  assert.equal(consumed.acknowledgement.grant_id, "grant-1");
  assert.equal(consumed.acknowledgement.resolved_model, "model-1");
  assert.equal(consumed.provider.route.base_url, "https://api.deepseek.com");
  consumed.secret.fill(0);
  assert.throws(() => grants.consumeForEnvelope(envelope), /unavailable/);
  assert.ok(secret.every(byte => byte === 0));
});


test("grant channel accepts a Broker-resolved model instead of the envelope alias", () => {
  const grants = new EphemeralGrantChannel(() => 100);
  grants.accept({
    grant_id: "grant-alias",
    grant_digest: "a".repeat(64),
    target_instance_digest: "b".repeat(64),
    command_id: "command-1",
    run_id: "run-1",
    term_id: "term-1",
    step_id: "step-1",
    provider_ref: "provider-profile:deepseek-primary",
    route: providerRoute(),
    model: "deepseek-reasoner",
    expires_at: 110,
  }, Buffer.from("fixture-secret", "utf8"));

  const consumed = grants.consumeForEnvelope({
    command_id: "command-1",
    run_id: "run-1",
    term_id: "term-1",
    step_id: "step-1",
    provider_ref: "provider-profile:deepseek-primary",
    model: "default",
  });
  assert.equal(consumed.acknowledgement.resolved_model, "deepseek-reasoner");
  consumed.secret.fill(0);
});


test("query consumes the shared Host v2 input and returns identity-bound seal acknowledgement", () => {
  const grants = new EphemeralGrantChannel(() => 100);
  grants.accept({
    grant_id: "grant-1",
    grant_digest: "a".repeat(64),
    target_instance_digest: "b".repeat(64),
    command_id: "start-1",
    run_id: "run-wire",
    term_id: "term-wire",
    step_id: "step-wire",
    provider_ref: "provider-profile:fixture-completed",
    route: providerRoute(),
    model: "fixture-model",
    expires_at: 110,
  }, Buffer.from("fixture-secret", "utf8"));
  const sidecar = createSidecar({
    grantChannel: grants,
    runtimeId: "dsh",
    buildId: "dsh:test-build",
    instanceDigest: "b".repeat(64),
  });

  const started = sidecar.startQuery(queryCommand().payload);

  assert.equal(started.accepted, true);
  assert.deepEqual(started.events.map(event => event.cursor), [1, 2, 3]);
  const checkpoint = createCheckpoint(started.events, "dsh:test-build");
  assert.equal(checkpoint.cursor, 3);
  assert.deepEqual(sidecar.checkpoint(), started.checkpoint);
  assert.deepEqual(
    sealAcknowledgement(started.events.at(-1), {
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 3,
    }),
    {
      state: "terminal",
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 3,
      sealed: true,
    },
  );
});


test("real provider query streams through the pinned DeepSeek Harness adapter", async () => {
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      requests.push({
        path: request.url,
        authorization: request.headers.authorization,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      });
      const body = [
        'data: {"choices":[{"delta":{"role":"assistant","content":null,"reasoning_content":""}}]}',
        'data: {"choices":[{"delta":{"content":"real "}}]}',
        'data: {"choices":[{"delta":{"content":"DSH"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}',
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
  const grants = new EphemeralGrantChannel(() => 100);
  grants.accept({
    grant_id: "grant-real",
    grant_digest: "a".repeat(64),
    target_instance_digest: "b".repeat(64),
    command_id: "start-1",
    run_id: "run-wire",
    term_id: "term-wire",
    step_id: "step-wire",
    provider_ref: "provider-profile:deepseek-primary",
    route: {
      protocol: "deepseek",
      base_url: `http://127.0.0.1:${address.port}`,
      credential_mode: "reference",
      metadata_headers: [],
      thinking_enabled: true,
      reasoning_effort: "high",
    },
    model: "deepseek-v4-flash",
    expires_at: 110,
  }, Buffer.from("one-shot-test-secret", "utf8"));
  const sidecar = createSidecar({
    grantChannel: grants,
    runtimeId: "dsh",
    buildId: "dsh:test-build",
    instanceDigest: "b".repeat(64),
  });
  const emitted = [];

  try {
    const started = sidecar.startQuery(
      queryCommand("provider-profile:deepseek-primary").payload,
      event => emitted.push(event),
    );
    assert.equal(started.accepted, true);
    assert.deepEqual(started.events.map(event => event.payload), [{ status: "running" }]);
    await started.completion;
  } finally {
    await new Promise(resolve => server.close(resolve));
  }

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/chat/completions");
  assert.equal(requests[0].authorization, "Bearer one-shot-test-secret");
  assert.equal(requests[0].body.model, "deepseek-v4-flash");
  assert.equal(requests[0].body.reasoning_effort, "high");
  assert.deepEqual(emitted.map(event => event.type), [
    "assistant.delta",
    "assistant.delta",
    "assistant.message",
    "runtime.status",
  ]);
  assert.deepEqual(emitted.at(-2).payload, { content: "real DSH" });
  assert.deepEqual(emitted.at(-1).payload, { status: "completed" });
  assert.equal(JSON.stringify(emitted).includes("one-shot-test-secret"), false);
});


test("pinned DeepSeek Harness Session preserves ordered input and native lifecycle", async () => {
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      const body = [
        'data: {"choices":[{"delta":{"role":"assistant","content":null,"reasoning_content":""}}]}',
        'data: {"choices":[{"delta":{"content":"native "}}]}',
        'data: {"choices":[{"delta":{"content":"session"},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":2}}',
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
  const nativeEvents = [];
  const materialized = {
    promptSections: [
      { section_id: "first", order: 10, content: "First instruction" },
      { section_id: "second", order: 20, content: "Second instruction" },
    ],
    contextItems: [{ item_id: "ctx-1", kind: "document", content: "Context evidence" }],
    messages: [
      { message_id: "m-1", role: "user", content: "Earlier question" },
      { message_id: "m-2", role: "assistant", content: "Earlier answer" },
      { message_id: "m-3", role: "user", content: "Current question" },
    ],
  };

  try {
    const result = await runDeepSeekHarnessSession({
      materialized,
      provider: {
        model: "deepseek-v4-flash",
        route: {
          protocol: "deepseek",
          base_url: `http://127.0.0.1:${address.port}`,
          credential_mode: "reference",
          metadata_headers: [],
          thinking_enabled: true,
          reasoning_effort: "high",
        },
      },
      credential: () => "native-session-secret",
      sessionId: "native-session-test",
      signal: new AbortController().signal,
      onEvent: event => nativeEvents.push(event.type),
    });
    assert.equal(result.content, "native session");
  } finally {
    await new Promise(resolve => server.close(resolve));
  }

  assert.equal(requests.length, 1);
  const conversationMessages = requests[0].messages.filter(message => message.role !== "system");
  assert.deepEqual(conversationMessages.map(message => message.role), [
    "user", "assistant", "user", "user",
  ]);
  assert.deepEqual(conversationMessages.slice(0, 3).map(message => message.content), [
    "Earlier question", "Earlier answer", "Current question",
  ]);
  assert.match(conversationMessages[3].content, /Context evidence$/);
  const system = requests[0].messages.find(message => message.role === "system")?.content;
  assert.match(system, /First instruction[\s\S]*Second instruction/);
  assert.ok(nativeEvents.includes("turn/start"));
  assert.ok(nativeEvents.includes("step/start"));
  assert.ok(nativeEvents.includes("assistant/chunk"));
  assert.ok(nativeEvents.includes("assistant/message"));
  assert.ok(nativeEvents.includes("turn/end"));
});


test("real provider query can be cancelled after the DeepSeek Harness request is in flight", async () => {
  let markRequestStarted;
  const requestStarted = new Promise(resolve => { markRequestStarted = resolve; });
  let releaseResponse;
  const responseReleased = new Promise(resolve => { releaseResponse = resolve; });
  const server = createServer((request, response) => {
    request.resume();
    request.on("end", async () => {
      markRequestStarted();
      await responseReleased;
      if (response.destroyed) return;
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.end("data: [DONE]\n\n");
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const grants = new EphemeralGrantChannel(() => 100);
  grants.accept({
    grant_id: "grant-real-cancel",
    grant_digest: "a".repeat(64),
    target_instance_digest: "b".repeat(64),
    command_id: "start-1",
    run_id: "run-wire",
    term_id: "term-wire",
    step_id: "step-wire",
    provider_ref: "provider-profile:deepseek-cancel",
    route: {
      protocol: "deepseek",
      base_url: `http://127.0.0.1:${address.port}`,
      credential_mode: "reference",
      metadata_headers: [],
      thinking_enabled: true,
      reasoning_effort: "high",
    },
    model: "deepseek-v4-flash",
    expires_at: 110,
  }, Buffer.from("cancel-test-secret", "utf8"));
  const sidecar = createSidecar({
    grantChannel: grants,
    runtimeId: "dsh",
    buildId: "dsh:test-build",
    instanceDigest: "b".repeat(64),
  });

  try {
    const started = sidecar.startQuery(
      queryCommand("provider-profile:deepseek-cancel").payload,
    );
    await requestStarted;
    const cancelled = sidecar.cancel("run-wire");
    assert.deepEqual(cancelled.payload, { status: "cancelled" });
    assert.equal(cancelled.cursor, 2);
    releaseResponse();
    await started.completion;
    assert.deepEqual(sidecar.seal({
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 2,
    }), {
      state: "terminal",
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 2,
      sealed: true,
    });
  } finally {
    releaseResponse();
    await new Promise(resolve => server.close(resolve));
  }
});


test("plugin smoke capabilities do not overclaim RF-4B controls or tools", () => {
  const sidecar = createSidecar({
    grantChannel: new EphemeralGrantChannel(),
    runtimeId: "dsh",
    buildId: "dsh:test-build",
    instanceDigest: "b".repeat(64),
  });

  const capabilities = sidecar.capabilities();
  assert.equal(capabilities.query, true);
  assert.equal(capabilities.streaming, true);
  assert.equal(capabilities.event_cursor, true);
  assert.equal(capabilities.model, false);
  assert.equal(capabilities.plugins, false);
  assert.equal(capabilities.checkpoints, false);
  assert.equal(capabilities.tools, false);
  assert.equal(capabilities.workspace, false);
  assert.equal(capabilities.interventions, false);
  assert.equal(capabilities.pause_resume, false);
  assert.equal(capabilities.compaction, false);
  assert.equal(capabilities.tool_interceptors, false);
});


test("published NDJSON sidecar accepts before events and seals completed terminal", async () => {
  const wire = await launchPublishedSidecar();
  try {
    wire.send({ type: "runtime.capabilities", command_id: "cap-1", payload: {} });
    const capabilities = await wire.read();
    assert.equal(capabilities.kind, "response");
    assert.equal(capabilities.payload.model, false);
    assert.equal(capabilities.payload.plugins, false);
    assert.equal(capabilities.payload.checkpoints, false);

    wire.send(queryCommand());
    const accepted = await wire.read();
    assert.deepEqual(accepted.payload, { accepted: true });
    assert.equal(accepted.command_id, "start-1");
    const events = [await wire.read(), await wire.read(), await wire.read()];
    assert.ok(events.every(frame => frame.kind === "event"));
    assert.deepEqual(events.map(frame => frame.payload.cursor), [1, 2, 3]);
    assert.equal(events[0].payload.payload.content, "system fixture\nhello from message\ncontext evidence");
    assert.equal(events.at(-1).payload.payload.status, "completed");

    wire.send({
      type: "query.status",
      command_id: "seal-1",
      payload: {
        run_id: "run-wire",
        term_id: "term-wire",
        step_id: "step-wire",
        terminal_cursor: 3,
      },
    });
    assert.deepEqual((await wire.read()).payload, {
      state: "terminal",
      run_id: "run-wire",
      term_id: "term-wire",
      step_id: "step-wire",
      terminal_cursor: 3,
      sealed: true,
    });
  } finally {
    await wire.close();
  }
});


test("published NDJSON sidecar streams a real local provider through pinned DeepSeek Harness", async () => {
  const requests = [];
  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      requests.push({
        path: request.url,
        authorization: request.headers.authorization,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      });
      const body = [
        'data: {"choices":[{"delta":{"role":"assistant","content":null,"reasoning_content":""}}]}',
        'data: {"choices":[{"delta":{"content":"published "}}]}',
        'data: {"choices":[{"delta":{"content":"DSH"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}',
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
  const providerRef = "provider-profile:deepseek-published";
  const grant = privateGrant(providerRef, "grant-real-published");
  grant.secret = Buffer.from("published-test-secret", "utf8");
  grant.binding.route.base_url = `http://127.0.0.1:${address.port}`;
  grant.binding.model = "deepseek-v4-flash";
  grant.grant_digest = digest(grant.binding);
  const wire = await launchPublishedSidecar(grant);
  try {
    wire.send(queryCommand(providerRef));
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    const events = [
      await wire.read(),
      await wire.read(),
      await wire.read(),
      await wire.read(),
      await wire.read(),
    ];
    assert.deepEqual(events.map(frame => frame.payload.type), [
      "runtime.status",
      "assistant.delta",
      "assistant.delta",
      "assistant.message",
      "runtime.status",
    ]);
    assert.deepEqual(events.at(-2).payload.payload, { content: "published DSH" });
    assert.deepEqual(events.at(-1).payload.payload, { status: "completed" });
  } finally {
    await wire.close();
    await new Promise(resolve => server.close(resolve));
  }
  assert.equal(requests[0].path, "/chat/completions");
  assert.equal(requests[0].authorization, "Bearer published-test-secret");
  assert.equal(requests[0].body.model, "deepseek-v4-flash");
});


test("published NDJSON sidecar accepts cancel while a real DSH request is in flight", async () => {
  let markRequestStarted;
  const requestStarted = new Promise(resolve => { markRequestStarted = resolve; });
  let releaseResponse;
  const responseReleased = new Promise(resolve => { releaseResponse = resolve; });
  const server = createServer((request, response) => {
    request.resume();
    request.on("end", async () => {
      markRequestStarted();
      await responseReleased;
      if (response.destroyed) return;
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.end("data: [DONE]\n\n");
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const providerRef = "provider-profile:deepseek-published-cancel";
  const grant = privateGrant(providerRef, "grant-real-published-cancel");
  grant.secret = Buffer.from("published-cancel-test-secret", "utf8");
  grant.binding.route.base_url = `http://127.0.0.1:${address.port}`;
  grant.binding.model = "deepseek-v4-flash";
  grant.grant_digest = digest(grant.binding);
  const wire = await launchPublishedSidecar(grant);
  try {
    wire.send(queryCommand(providerRef));
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    assert.deepEqual((await wire.read()).payload.payload, { status: "running" });
    await requestStarted;
    wire.send({
      type: "query.cancel",
      command_id: "cancel-real-published",
      payload: { run_id: "run-wire", reason: "user_requested" },
    });
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    const cancelled = await wire.read();
    assert.deepEqual(cancelled.payload.payload, { status: "cancelled" });
    assert.equal(cancelled.payload.cursor, 2);
  } finally {
    releaseResponse();
    await wire.close();
    await new Promise(resolve => server.close(resolve));
  }
});


test("published sidecar binds a Supervisor-issued runtime instance instead of a fixture digest", async () => {
  const supervisorInstanceDigest = "a".repeat(64);
  const wire = await launchPublishedSidecar(privateGrant(
    "provider-profile:fixture-completed",
    "grant-supervisor-instance",
    supervisorInstanceDigest,
  ));
  try {
    wire.send(queryCommand());
    assert.deepEqual((await wire.read()).payload, { accepted: true });
  } finally {
    await wire.close();
  }
});


test("published NDJSON sidecar emits failed terminal from the fixed provider after acceptance", async () => {
  const providerRef = "provider-profile:fixture-failed";
  const wire = await launchPublishedSidecar(privateGrant(providerRef));
  try {
    wire.send(queryCommand(providerRef));
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    const terminal = await wire.read();
    assert.equal(terminal.kind, "event");
    assert.equal(terminal.payload.payload.status, "failed");
    assert.equal(terminal.payload.cursor, 1);
  } finally {
    await wire.close();
  }
});


test("published NDJSON sidecar cancels a fixed held provider query and validates seal identity", async () => {
  const providerRef = "provider-profile:fixture-held";
  const wire = await launchPublishedSidecar(privateGrant(providerRef));
  try {
    wire.send(queryCommand(providerRef));
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    const running = await wire.read();
    assert.equal(running.payload.payload.status, "running");

    wire.send({
      type: "query.cancel",
      command_id: "cancel-1",
      payload: { run_id: "run-wire", reason: "user_requested" },
    });
    assert.deepEqual((await wire.read()).payload, { accepted: true });
    const cancelled = await wire.read();
    assert.equal(cancelled.payload.payload.status, "cancelled");
    assert.equal(cancelled.payload.cursor, 2);

    wire.send({
      type: "query.status",
      command_id: "bad-seal",
      payload: {
        run_id: "other-run",
        term_id: "term-wire",
        step_id: "step-wire",
        terminal_cursor: 2,
      },
    });
    assert.match((await wire.read()).payload.error, /identity/);
  } finally {
    await wire.close();
  }
});

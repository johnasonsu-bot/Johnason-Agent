import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import path from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadFixedPreset } from "../src/bootstrap.ts";
import { createCheckpoint, sealAcknowledgement } from "../src/checkpoint.ts";
import { mapSessionEvents, sortPromptSections } from "../src/event-mapper.ts";
import { EphemeralGrantChannel } from "../src/grant-channel.ts";
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


function privateGrant(providerRef = "provider-profile:fixture-completed", grantId = "grant-wire") {
  const { envelope } = queryCommand(providerRef).payload;
  const issuedAt = Date.now() / 1000;
  const binding = {
    grant_id: grantId,
    target: {
      runtime_id: "dsh",
      build_id: "dsh:fixed-host-v2-smoke",
      lease_id: "lease-wire",
      instance_id_digest: INSTANCE_DIGEST,
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
    target_instance_digest: INSTANCE_DIGEST,
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
  assert.equal(receipt.artifacts.length, 6);
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

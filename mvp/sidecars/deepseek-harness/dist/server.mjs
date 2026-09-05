#!/usr/bin/env node
import { createHash } from "node:crypto";
import { createInterface } from "node:readline";

import { createCheckpoint, sealAcknowledgement } from "./checkpoint.mjs";
import { mapSessionEvents, sortPromptSections } from "./event-mapper.mjs";
import { runDeepSeekHarnessSession } from "./native-session.mjs";


const DIGEST = /^[0-9a-f]{64}$/;
const RUNTIME_INPUT_FIELDS = Object.freeze([
  "messages", "message_snapshot_digest", "context_items", "context_snapshot_digest",
  "prompt_sections", "prompt_manifest_digest",
]);


function exactKeys(value, expected) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === [...expected].sort().join(",");
}


function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}


function inputDigest(items) {
  return createHash("sha256").update(JSON.stringify(canonical(items))).digest("hex");
}


function materializeRuntimeInput(runtimeInput, envelope) {
  if (!exactKeys(runtimeInput, RUNTIME_INPUT_FIELDS)
      || !Array.isArray(runtimeInput.messages) || runtimeInput.messages.length === 0
      || !Array.isArray(runtimeInput.context_items)
      || !Array.isArray(runtimeInput.prompt_sections)) {
    throw new Error("DSH runtime input does not match RuntimeQueryInputV2");
  }
  const messages = runtimeInput.messages.map(message => {
    if (!exactKeys(message, ["message_id", "role", "content"])
        || typeof message.message_id !== "string" || message.message_id.length === 0
        || !["system", "user", "assistant", "tool"].includes(message.role)
        || typeof message.content !== "string") {
      throw new Error("DSH runtime message is invalid");
    }
    return Object.freeze({ ...message });
  });
  const contextItems = runtimeInput.context_items.map(item => {
    if (!exactKeys(item, ["item_id", "kind", "content"])
        || typeof item.item_id !== "string" || item.item_id.length === 0
        || typeof item.kind !== "string" || item.kind.length === 0
        || typeof item.content !== "string") {
      throw new Error("DSH runtime context item is invalid");
    }
    return Object.freeze({ ...item });
  });
  if (runtimeInput.prompt_sections.some(
    section => !exactKeys(section, ["section_id", "order", "content"]),
  )) {
    throw new Error("DSH prompt section shape is invalid");
  }
  const promptSections = sortPromptSections(runtimeInput.prompt_sections);
  const identities = [
    messages.map(item => item.message_id),
    contextItems.map(item => item.item_id),
    promptSections.map(item => item.section_id),
  ];
  if (identities.some(values => new Set(values).size !== values.length)) {
    throw new Error("DSH runtime input contains duplicate identities");
  }
  const expectedDigests = [
    [runtimeInput.message_snapshot_digest, inputDigest(messages)],
    [runtimeInput.context_snapshot_digest, inputDigest(contextItems)],
    [runtimeInput.prompt_manifest_digest, inputDigest(promptSections)],
  ];
  if (expectedDigests.some(([actual, expected]) => !DIGEST.test(actual) || actual !== expected)
      || envelope.message_snapshot_digest !== runtimeInput.message_snapshot_digest
      || envelope.context?.snapshot_digest !== runtimeInput.context_snapshot_digest
      || envelope.prompt_manifest_digest !== runtimeInput.prompt_manifest_digest) {
    throw new Error("DSH runtime input digest binding is invalid");
  }
  return Object.freeze({ messages, contextItems, promptSections });
}


function fixedProviderOutcome(providerRef) {
  const prefix = "provider-profile:fixture-";
  if (typeof providerRef !== "string" || !providerRef.startsWith(prefix)) {
    throw new Error("DSH fixed smoke provider is unsupported");
  }
  const outcome = providerRef.slice(prefix.length);
  if (!["completed", "failed", "held"].includes(outcome)) {
    throw new Error("DSH fixed smoke provider is unsupported");
  }
  return outcome;
}


function isFixedProvider(providerRef) {
  return typeof providerRef === "string"
    && providerRef.startsWith("provider-profile:fixture-");
}


export function createSidecar({ grantChannel, runtimeId, buildId, instanceDigest }) {
  if (!grantChannel || typeof runtimeId !== "string" || typeof buildId !== "string") {
    throw new Error("DSH Host v2 bootstrap is invalid");
  }
  let terminal = null;
  let checkpoint = null;
  let active = null;

  return Object.freeze({
    capabilities() {
      return Object.freeze({
        runtime_id: runtimeId,
        build_id: buildId,
        protocol_version: "2.0",
        query: true,
        model: buildId === "dsh:model-host-v2-r1",
        tools: false,
        skills: false,
        plugins: false,
        workspace: false,
        interventions: false,
        pause_resume: false,
        compaction: false,
        checkpoints: false,
        streaming: true,
        plan: false,
        todo: false,
        prompt_sections: true,
        tool_interceptors: false,
        event_cursor: true,
      });
    },

    startQuery(payload, emit = () => {}) {
      if (active !== null) throw new Error("a DSH query is already active");
      const envelope = payload?.envelope;
      const runtimeInput = payload?.runtime_input;
      if (!envelope || !runtimeInput) {
        throw new Error("DSH query input is incomplete");
      }
      const materialized = materializeRuntimeInput(runtimeInput, envelope);
      if (buildId === "dsh:model-host-v2-r1" && isFixedProvider(envelope.provider_ref)) {
        throw new Error("model Host rejects fixture provider");
      }
      const consumedGrant = grantChannel.consumeForEnvelope(envelope);
      const secret = consumedGrant.secret;
      terminal = null;
      checkpoint = null;
      if (typeof instanceDigest === "string" && instanceDigest.length !== 64) {
        secret.fill(0);
        throw new Error("DSH instance digest is invalid");
      }
      if (consumedGrant.acknowledgement.command_id !== envelope.command_id
          || consumedGrant.acknowledgement.run_id !== envelope.run_id
          || consumedGrant.acknowledgement.term_id !== envelope.term_id
          || consumedGrant.acknowledgement.step_id !== envelope.step_id) {
        secret.fill(0);
        throw new Error("DSH provider grant acknowledgement identity is invalid");
      }
      if (!isFixedProvider(envelope.provider_ref)) {
        let credential = secret.toString("utf8");
        secret.fill(0);
        const identity = Object.freeze({
          run_id: envelope.run_id,
          term_id: envelope.term_id,
          step_id: envelope.step_id,
        });
        const [running] = mapSessionEvents(identity, [
          { seq: 0, type: "turn/start", data: {} },
        ]);
        const controller = new AbortController();
        active = {
          identity,
          cursor: running.cursor,
          controller,
          events: [running],
        };
        const completion = (async () => {
          try {
            const result = await runDeepSeekHarnessSession({
              materialized,
              provider: consumedGrant.provider,
              credential: () => credential,
              sessionId: envelope.session_id,
              signal: controller.signal,
              onEvent: nativeEvent => {
                const chunk = nativeEvent.type === "assistant/chunk"
                  ? nativeEvent.data?.chunk
                  : null;
                if (active === null || active.identity !== identity) return;
                if (chunk?.type === "text-delta" && chunk.text.length !== 0) {
                  const [event] = mapSessionEvents(identity, [
                    { seq: 0, type: "assistant/delta", data: { content: chunk.text } },
                  ], { cursorOffset: active.cursor });
                  active.cursor = event.cursor;
                  active.events.push(event);
                  emit(event);
                }
              },
            });
            if (active === null || active.identity !== identity) return;
            const completed = mapSessionEvents(identity, [
              { seq: 0, type: "assistant/message", data: { content: result.content } },
              { seq: 1, type: "turn/end", data: { reason: "completed" } },
            ], { cursorOffset: active.cursor });
            active.events.push(...completed);
            terminal = completed.at(-1);
            checkpoint = createCheckpoint(active.events, buildId);
            active = null;
            for (const event of completed) emit(event);
          } catch (error) {
            if (active === null || active.identity !== identity || controller.signal.aborted) {
              return;
            }
            const [failed] = mapSessionEvents(identity, [
              { seq: 0, type: "turn/end", data: { reason: "failed" } },
            ], { cursorOffset: active.cursor });
            terminal = failed;
            checkpoint = null;
            active = null;
            emit(failed);
          } finally {
            credential = "";
          }
        })();
        return Object.freeze({
          accepted: true,
          events: Object.freeze([running]),
          checkpoint: null,
          completion,
        });
      }
      const outcome = fixedProviderOutcome(envelope.provider_ref);
      try {
        const content = [
          ...materialized.promptSections.map(section => section.content),
          ...materialized.messages.map(message => message.content),
          ...materialized.contextItems.map(item => item.content),
        ].join("\n");
        if (outcome === "failed") {
          const events = mapSessionEvents(envelope, [
            { seq: 0, type: "turn/end", data: { reason: "failed" } },
          ]);
          terminal = events.at(-1);
          checkpoint = null;
          return Object.freeze({ accepted: true, events, checkpoint: null });
        }
        if (outcome === "held") {
          const events = mapSessionEvents(envelope, [
            { seq: 0, type: "turn/start", data: {} },
          ]);
          terminal = null;
          checkpoint = null;
          active = Object.freeze({
            identity: Object.freeze({
              run_id: envelope.run_id,
              term_id: envelope.term_id,
              step_id: envelope.step_id,
            }),
            cursor: events.at(-1).cursor,
          });
          return Object.freeze({ accepted: true, events, checkpoint: null });
        }
        const events = mapSessionEvents(envelope, [
          { seq: 0, type: "assistant/delta", data: { content } },
          { seq: 1, type: "assistant/message", data: { content } },
          { seq: 2, type: "turn/end", data: { reason: "completed" } },
        ]);
        terminal = events.at(-1);
        checkpoint = createCheckpoint(events, buildId);
        return Object.freeze({ accepted: true, events, checkpoint });
      } finally {
        secret.fill(0);
      }
    },

    cancel(runId) {
      if (active === null || active.identity.run_id !== runId) {
        throw new Error("cancel identity does not match an active DSH query");
      }
      active.controller?.abort("Host v2 query.cancel");
      const [event] = mapSessionEvents(
        active.identity,
        [{ seq: 0, type: "turn/end", data: { reason: "cancelled" } }],
        { cursorOffset: active.cursor },
      );
      active = null;
      terminal = event;
      checkpoint = null;
      return event;
    },

    seal(requested) {
      return sealAcknowledgement(terminal, requested);
    },

    checkpoint() {
      if (checkpoint === null) throw new Error("DSH checkpoint is unavailable");
      return checkpoint;
    },

    shutdown() {
      grantChannel.clear();
    },
  });
}


export function serveNdjson(
  sidecar,
  input = process.stdin,
  output = process.stdout,
  grantReady = Promise.resolve(),
) {
  const lines = createInterface({ input, crlfDelay: Infinity });
  let commands = Promise.resolve();
  const handleLine = async line => {
    let command;
    try {
      command = JSON.parse(line);
      if (command?.kind !== "command" || typeof command.type !== "string") {
        throw new Error("invalid Host v2 command");
      }
      let payload;
      if (command.type === "runtime.capabilities") {
        payload = sidecar.capabilities();
      } else if (command.type === "query.start") {
        await grantReady;
        const started = sidecar.startQuery(command.payload, event => {
          output.write(`${JSON.stringify({ kind: "event", payload: event })}\n`);
        });
        output.write(`${JSON.stringify({
          kind: "response",
          type: command.type,
          command_id: command.command_id,
          payload: { accepted: started.accepted },
        })}\n`);
        for (const event of started.events) {
          output.write(`${JSON.stringify({ kind: "event", payload: event })}\n`);
        }
        return;
      } else if (command.type === "query.cancel") {
        const event = sidecar.cancel(command.payload?.run_id);
        output.write(`${JSON.stringify({
          kind: "response",
          type: command.type,
          command_id: command.command_id,
          payload: { accepted: true },
        })}\n`);
        output.write(`${JSON.stringify({ kind: "event", payload: event })}\n`);
        return;
      } else if (command.type === "query.status") {
        payload = sidecar.seal(command.payload);
      } else {
        throw new Error("unsupported Host v2 command");
      }
      output.write(`${JSON.stringify({
        kind: "response",
        type: command.type,
        command_id: command.command_id,
        payload,
      })}\n`);
    } catch (error) {
      output.write(`${JSON.stringify({
        kind: "response",
        type: command?.type ?? "invalid",
        command_id: command?.command_id ?? "invalid",
        payload: { error: error instanceof Error ? error.message : "sidecar failure" },
      })}\n`);
    }
  };
  lines.on("line", line => {
    commands = commands.then(() => handleLine(line));
  });
  lines.on("close", () => {
    void commands.finally(() => sidecar.shutdown());
  });
  return lines;
}

#!/usr/bin/env node
import { createHash } from "node:crypto";
import { createInterface } from "node:readline";

import { createCheckpoint, sealAcknowledgement } from "./checkpoint.ts";
import { mapSessionEvents, sortPromptSections } from "./event-mapper.ts";
import { runDeepSeekHarnessSession, DshSessionFailure } from "./native-session.ts";


const DIGEST = /^[0-9a-f]{64}$/;
const RUNTIME_INPUT_FIELDS = Object.freeze([
  "messages", "message_snapshot_digest", "context_items", "context_snapshot_digest",
  "prompt_sections", "prompt_manifest_digest",
]);
const PLATFORM_RUN_IDENTITY_FIELDS = Object.freeze([
  "session_id", "run_id", "term_id", "step_id", "command_id", "agent_id", "agent_role",
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


function validRunIdentity(identity) {
  return exactKeys(identity, PLATFORM_RUN_IDENTITY_FIELDS)
    && PLATFORM_RUN_IDENTITY_FIELDS.every(
      field => typeof identity[field] === "string" && identity[field].length !== 0,
    );
}


function sameRunIdentity(actual, expected) {
  return validRunIdentity(actual) && validRunIdentity(expected)
    && PLATFORM_RUN_IDENTITY_FIELDS.every(field => actual[field] === expected[field]);
}


function createNdjsonToolTransport(output) {
  let sequence = 0;
  let closed = null;
  const pending = new Map();
  const writeRequest = frame => {
    output.write(`${JSON.stringify(frame)}\n`);
  };
  return Object.freeze({
    execute(call) {
      if (!call || typeof call !== "object"
          || !validRunIdentity(call.identity)
          || typeof call.tool_call_id !== "string" || call.tool_call_id.length === 0
          || typeof call.tool_id !== "string" || call.tool_id.length === 0
          || call.arguments === null || typeof call.arguments !== "object"
          || Array.isArray(call.arguments)
          || !(call.signal instanceof AbortSignal)) {
        throw new Error("DSH platform tool execution request is invalid");
      }
      if (closed !== null) throw closed;
      const requestId = `dsh-tool-${++sequence}`;
      const identity = Object.freeze(structuredClone(call.identity));
      const toolCallId = call.tool_call_id;
      const toolId = call.tool_id;
      return new Promise((resolve, reject) => {
        const entry = {
          identity,
          toolCallId,
          toolId,
          signal: call.signal,
          resolve,
          reject,
          cancelSent: false,
          onAbort: null,
        };
        entry.onAbort = () => {
          if (entry.cancelSent || !pending.has(requestId)) return;
          entry.cancelSent = true;
          writeRequest({
            kind: "request",
            type: "tool.cancel",
            request_id: requestId,
            payload: {
              identity,
              tool_call_id: toolCallId,
              tool_id: toolId,
            },
          });
        };
        pending.set(requestId, entry);
        call.signal.addEventListener("abort", entry.onAbort, { once: true });
        writeRequest({
          kind: "request",
          type: "tool.execute",
          request_id: requestId,
          payload: {
            identity,
            tool_call_id: toolCallId,
            tool_id: toolId,
            arguments: structuredClone(call.arguments),
          },
        });
        if (call.signal.aborted) entry.onAbort();
      });
    },

    deliver(command) {
      if (!exactKeys(command, ["kind", "type", "command_id", "payload"])
          || command.kind !== "command" || command.type !== "tool.result"
          || typeof command.command_id !== "string" || command.command_id.length === 0) {
        throw new Error("DSH platform tool result command is invalid");
      }
      const requestId = command.command_id;
      const entry = pending.get(requestId);
      if (!entry) {
        throw new Error("DSH platform tool result is unknown or duplicate");
      }
      const payload = command.payload;
      if (!exactKeys(payload, [
        "identity", "tool_call_id", "tool_id", "status", "output", "effect_id",
      ])
          || !sameRunIdentity(payload.identity, entry.identity)
          || payload.tool_call_id !== entry.toolCallId
          || payload.tool_id !== entry.toolId
          || !["completed", "failed"].includes(payload.status)
          || !(payload.effect_id === null || typeof payload.effect_id === "string")) {
        throw new Error("DSH platform tool result correlation is invalid");
      }
      pending.delete(requestId);
      entry.signal.removeEventListener("abort", entry.onAbort);
      entry.resolve(Object.freeze({
        status: payload.status,
        output: payload.output,
        ...(typeof payload.effect_id === "string" && payload.effect_id.length !== 0
          ? { effect_id: payload.effect_id } : {}),
      }));
    },

    close(reason = new Error("DSH tool transport closed")) {
      if (closed !== null) return;
      closed = reason instanceof Error ? reason : new Error("DSH tool transport closed");
      for (const entry of pending.values()) {
        entry.signal.removeEventListener("abort", entry.onAbort);
        entry.reject(closed);
      }
      pending.clear();
    },
  });
}


export function createSidecar({ grantChannel, runtimeId, buildId, instanceDigest }) {
  if (!grantChannel || typeof runtimeId !== "string" || typeof buildId !== "string") {
    throw new Error("DSH Host v2 bootstrap is invalid");
  }
  let terminal = null;
  let checkpoint = null;
  let active = null;
  let toolTransport = null;
  let shutdownPromise = null;
  let closed = false;

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

    attachToolTransport(transport) {
      if (toolTransport !== null || !transport
          || typeof transport.execute !== "function"
          || typeof transport.deliver !== "function"
          || typeof transport.close !== "function") {
        throw new Error("DSH tool transport attachment is invalid");
      }
      toolTransport = transport;
    },

    deliverToolResult(command) {
      if (toolTransport === null) {
        throw new Error("DSH tool transport is unavailable");
      }
      toolTransport.deliver(command);
    },

    startQuery(payload, emit = () => {}) {
      if (closed) throw new Error("DSH sidecar is shut down");
      if (active !== null) throw new Error("a DSH query is already active");
      const envelope = payload?.envelope;
      const runtimeInput = payload?.runtime_input;
      if (!envelope || !runtimeInput || !Array.isArray(envelope.tool_manifest)) {
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
        const runIdentity = Object.freeze({
          session_id: envelope.session_id,
          run_id: envelope.run_id,
          term_id: envelope.term_id,
          step_id: envelope.step_id,
          command_id: envelope.command_id,
          agent_id: envelope.agent_id,
          agent_role: envelope.agent_role,
        });
        const [running] = mapSessionEvents(identity, [
          { seq: 0, type: "turn/start", data: {} },
        ]);
        const controller = new AbortController();
        const state = {
          identity,
          cursor: running.cursor,
          controller,
          events: [running],
          completion: null,
          cancelPromise: null,
        };
        active = state;
        const completion = (async () => {
          try {
            const result = await runDeepSeekHarnessSession({
              materialized,
              provider: consumedGrant.provider,
              credential: () => credential,
              sessionId: envelope.session_id,
              signal: controller.signal,
              toolManifest: envelope.tool_manifest,
              runIdentity,
              executeTool: call => {
                if (toolTransport === null) {
                  throw new Error("DSH platform tool transport is unavailable");
                }
                return toolTransport.execute(call);
              },
              onEvent: nativeEvent => {
                const chunk = nativeEvent.type === "assistant/chunk"
                  ? nativeEvent.data?.chunk
                  : null;
                if (active !== state) return;
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
            if (active !== state) return;
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
            if (active !== state) return;
            const cancelled = controller.signal.aborted;
            const [finalEvent] = mapSessionEvents(identity, [
              { seq: 0, type: "turn/end", data: {
                reason: cancelled ? "cancelled" : "failed",
                ...(!cancelled
                  ? (error instanceof DshSessionFailure
                    ? error.publicFailure
                    : {
                      reason_code: "runtime_verification_failed",
                      failure_stage: "session_execution",
                    })
                  : {}),
              } },
            ], { cursorOffset: active.cursor });
            terminal = finalEvent;
            checkpoint = null;
            active = null;
            if (!cancelled) emit(finalEvent);
          } finally {
            credential = "";
          }
        })();
        state.completion = completion;
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

    async cancel(runId) {
      if (active === null || active.identity.run_id !== runId) {
        throw new Error("cancel identity does not match an active DSH query");
      }
      const state = active;
      if (state.controller) {
        if (state.cancelPromise === null) {
          state.cancelPromise = (async () => {
            state.controller.abort(new Error("Host v2 query.cancel"));
            await state.completion;
            if (terminal === null || terminal.payload?.status !== "cancelled") {
              throw new Error("DSH query cancellation did not settle");
            }
            return terminal;
          })();
        }
        return state.cancelPromise;
      }
      const [event] = mapSessionEvents(
        state.identity,
        [{ seq: 0, type: "turn/end", data: { reason: "cancelled" } }],
        { cursorOffset: state.cursor },
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

    shutdown(reason = new Error("DSH NDJSON input closed")) {
      if (shutdownPromise !== null) return shutdownPromise;
      closed = true;
      shutdownPromise = (async () => {
        const state = active;
        active = null;
        toolTransport?.close(reason);
        try {
          if (state?.controller) {
            state.controller.abort(reason);
            await state.completion;
          }
        } finally {
          grantChannel.clear();
        }
      })();
      return shutdownPromise;
    },
  });
}


export function serveNdjson(
  sidecar,
  input = process.stdin,
  output = process.stdout,
  grantReady = Promise.resolve(),
) {
  const toolTransport = createNdjsonToolTransport(output);
  sidecar.attachToolTransport(toolTransport);
  const lines = createInterface({ input, crlfDelay: Infinity });
  let commands = Promise.resolve();
  let inputClosed = false;
  const handleLine = async line => {
    let command;
    try {
      if (inputClosed) return;
      command = JSON.parse(line);
      if (command?.kind !== "command" || typeof command.type !== "string") {
        throw new Error("invalid Host v2 command");
      }
      let payload;
      if (command.type === "runtime.capabilities") {
        payload = sidecar.capabilities();
      } else if (command.type === "query.start") {
        await grantReady;
        if (inputClosed) return;
        const started = sidecar.startQuery(command.payload, event => {
          if (!inputClosed) {
            output.write(`${JSON.stringify({ kind: "event", payload: event })}\n`);
          }
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
        const event = await sidecar.cancel(command.payload?.run_id);
        if (inputClosed) return;
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
      if (inputClosed) return;
      output.write(`${JSON.stringify({
        kind: "response",
        type: command?.type ?? "invalid",
        command_id: command?.command_id ?? "invalid",
        payload: { error: error instanceof Error ? error.message : "sidecar failure" },
      })}\n`);
    }
  };
  lines.on("line", line => {
    let command;
    try {
      command = JSON.parse(line);
    } catch {
      // Preserve the existing invalid-command response path.
    }
    if (command?.type === "tool.result") {
      try {
        sidecar.deliverToolResult(command);
      } catch {
        // tool.result is one-way: invalid, mismatched, and duplicate deliveries
        // are rejected without a response acknowledgement.
      }
      return;
    }
    commands = commands.then(() => handleLine(line));
  });
  lines.on("close", () => {
    inputClosed = true;
    void sidecar.shutdown(new Error("DSH NDJSON input closed"));
  });
  return lines;
}

import { createHash } from "node:crypto";

const TERMINAL_STATUS = Object.freeze({
  completed: ["completed", "query.completed"],
  failed: ["failed", "query.failed"],
  cancelled: ["cancelled", "query.cancelled"],
  canceled: ["cancelled", "query.cancelled"],
});


function requireIdentity(identity) {
  for (const field of ["run_id", "term_id", "step_id"]) {
    if (typeof identity?.[field] !== "string" || identity[field].length === 0) {
      throw new Error(`DSH event identity ${field} is invalid`);
    }
  }
}


export function sortPromptSections(sections) {
  if (!Array.isArray(sections)) throw new Error("prompt sections must be an array");
  return sections.map(section => {
    if (!section || !Number.isSafeInteger(section.order)
        || typeof section.section_id !== "string" || section.section_id.length === 0
        || typeof section.content !== "string") {
      throw new Error("prompt section is invalid");
    }
    return Object.freeze({
      section_id: section.section_id,
      order: section.order,
      content: section.content,
    });
  }).sort((left, right) => {
    if (left.order !== right.order) return left.order - right.order;
    if (left.section_id < right.section_id) return -1;
    if (left.section_id > right.section_id) return 1;
    return 0;
  });
}


function mapPayload(event) {
  if (event.type === "assistant/delta") {
    return ["assistant.delta", { content: String(event.data?.content ?? "") }];
  }
  if (event.type === "assistant/message") {
    return ["assistant.message", { content: String(event.data?.content ?? "") }];
  }
  if (event.type === "turn/start") {
    return ["runtime.status", { status: "running" }];
  }
  if (event.type === "tool/call") {
    return ["tool.call", { ...event.data }];
  }
  if (event.type === "tool/result") {
    return ["tool.result", { ...event.data }];
  }
  if (event.type === "turn/end") {
    const terminal = TERMINAL_STATUS[event.data?.reason];
    if (!terminal) throw new Error("DSH terminal reason is unsupported");
    const diagnostics = {};
    if (terminal[0] === "failed") {
      if (["provider_request_failed", "runtime_verification_failed"].includes(event.data?.reason_code)) {
        diagnostics.reason_code = event.data.reason_code;
      }
      if (["provider_request", "session_setup", "session_execution"].includes(event.data?.failure_stage)) {
        diagnostics.failure_stage = event.data.failure_stage;
      }
      if ([400, 401, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504].includes(event.data?.http_status)) {
        diagnostics.http_status = event.data.http_status;
      }
    }
    return ["runtime.status", { status: terminal[0], ...diagnostics }];
  }
  return ["vendor.dsh.event", { dsh_type: event.type }];
}


export function mapSessionEvents(identity, events, { cursorOffset = 0 } = {}) {
  requireIdentity(identity);
  if (!Array.isArray(events)) throw new Error("DSH events must be an array");
  if (!Number.isSafeInteger(cursorOffset) || cursorOffset < 0) {
    throw new Error("DSH event cursor offset is invalid");
  }
  return events.map((event, index) => {
    if (!event || event.seq !== index) {
      throw new Error(`DSH event sequence gap at ${index}`);
    }
    if (typeof event.type !== "string" || event.type.length === 0) {
      throw new Error("DSH event type is invalid");
    }
    const [type, payload] = mapPayload(event);
    return Object.freeze({
      event_id: `dsh:${createHash("sha256").update(JSON.stringify([
        "dsh-event-v1", identity.run_id, identity.term_id, identity.step_id,
        cursorOffset + index + 1,
      ])).digest("hex")}`,
      run_id: identity.run_id,
      term_id: identity.term_id,
      step_id: identity.step_id,
      cursor: cursorOffset + event.seq + 1,
      type,
      payload: Object.freeze(payload),
      required: type === "runtime.status",
    });
  });
}

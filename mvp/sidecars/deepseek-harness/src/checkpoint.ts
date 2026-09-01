import { createHash } from "node:crypto";


function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}


export function createCheckpoint(events, buildId) {
  if (!Array.isArray(events) || events.length === 0 || typeof buildId !== "string") {
    throw new Error("checkpoint input is invalid");
  }
  const last = events.at(-1);
  const evidence = JSON.stringify(canonical({
    build_id: buildId,
    run_id: last.run_id,
    term_id: last.term_id,
    step_id: last.step_id,
    cursor: last.cursor,
    events,
  }));
  const checkpointDigest = createHash("sha256").update(evidence).digest("hex");
  return Object.freeze({
    checkpoint_ref: `dsh-checkpoint:${last.step_id}:${last.cursor}`,
    checkpoint_digest: checkpointDigest,
    cursor: last.cursor,
  });
}


export function sealAcknowledgement(terminal, requested) {
  if (!terminal || terminal.type !== "runtime.status"
      || !["completed", "failed", "cancelled"].includes(terminal.payload?.status)) {
    throw new Error("terminal event cannot be sealed");
  }
  if (!requested
      || requested.run_id !== terminal.run_id
      || requested.term_id !== terminal.term_id
      || requested.step_id !== terminal.step_id
      || requested.terminal_cursor !== terminal.cursor) {
    throw new Error("terminal seal identity mismatch");
  }
  return Object.freeze({
    state: "terminal",
    run_id: terminal.run_id,
    term_id: terminal.term_id,
    step_id: terminal.step_id,
    terminal_cursor: terminal.cursor,
    sealed: true,
  });
}

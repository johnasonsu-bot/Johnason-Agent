import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";


const DIGEST = /^[0-9a-f]{64}$/;
const BINDING_IDENTITY_FIELDS = Object.freeze([
  "command_id", "run_id", "term_id", "step_id", "provider_ref", "model",
]);


function exactKeys(value, expected) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === [...expected].sort().join(",");
}


export class EphemeralGrantChannel {
  constructor(clock = () => Date.now() / 1000, targetInstanceDigest = null) {
    this.clock = clock;
    this.targetInstanceDigest = targetInstanceDigest;
    this.grants = new Map();
  }

  accept(binding, transientSecret) {
    if (!binding || typeof binding.grant_id !== "string" || binding.grant_id.length === 0
        || !DIGEST.test(binding.grant_digest)
        || !DIGEST.test(binding.target_instance_digest)
        || (this.targetInstanceDigest !== null
          && binding.target_instance_digest !== this.targetInstanceDigest)
        || typeof binding.expires_at !== "number"
        || !Number.isFinite(binding.expires_at)
        || binding.expires_at <= this.clock()
        || BINDING_IDENTITY_FIELDS.some(
          field => typeof binding[field] !== "string" || binding[field].length === 0,
        )) {
      throw new Error("provider grant binding is invalid or expired");
    }
    if (!Buffer.isBuffer(transientSecret) || transientSecret.length === 0) {
      throw new Error("provider grant secret is invalid");
    }
    if (this.grants.has(binding.grant_id)) {
      throw new Error("provider grant replay is forbidden");
    }
    const retained = Buffer.from(transientSecret);
    transientSecret.fill(0);
    this.grants.set(binding.grant_id, { binding: Object.freeze({ ...binding }), secret: retained });
    return Object.freeze({
      grant_id: binding.grant_id,
      grant_digest: binding.grant_digest,
      target_instance_digest: binding.target_instance_digest,
      acknowledged_at: this.clock(),
    });
  }

  consumeForEnvelope(envelope) {
    if (!envelope || BINDING_IDENTITY_FIELDS.some(
      field => typeof envelope[field] !== "string" || envelope[field].length === 0,
    )) {
      throw new Error("provider grant envelope identity is invalid");
    }
    for (const [grantId, entry] of this.grants) {
      if (entry.binding.expires_at <= this.clock()) {
        entry.secret.fill(0);
        this.grants.delete(grantId);
      }
    }
    const matching = [...this.grants.entries()].filter(([, entry]) => (
      BINDING_IDENTITY_FIELDS.every(field => entry.binding[field] === envelope[field])
    ));
    if (matching.length !== 1) {
      throw new Error("provider grant is unavailable");
    }
    const [grantId, entry] = matching[0];
    this.grants.delete(grantId);
    return Object.freeze({
      secret: entry.secret,
      acknowledgement: Object.freeze({
        grant_id: entry.binding.grant_id,
        grant_digest: entry.binding.grant_digest,
        target_instance_digest: entry.binding.target_instance_digest,
        command_id: entry.binding.command_id,
        run_id: entry.binding.run_id,
        term_id: entry.binding.term_id,
        step_id: entry.binding.step_id,
        acknowledged_at: this.clock(),
      }),
    });
  }

  clear() {
    for (const { secret } of this.grants.values()) secret.fill(0);
    this.grants.clear();
  }

  static digest(binding) {
    return createHash("sha256").update(JSON.stringify(binding)).digest("hex");
  }
}


export function readPreopenedGrantChannel(
  fd,
  targetInstanceDigest,
  clock = () => Date.now() / 1000,
) {
  if (!Number.isSafeInteger(fd) || fd <= 2 || !DIGEST.test(targetInstanceDigest)) {
    throw new Error("private provider grant descriptor is invalid");
  }
  let payload;
  try {
    payload = readFileSync(fd, "utf8");
  } catch {
    throw new Error("private provider grant descriptor is unavailable");
  }
  const channel = new EphemeralGrantChannel(clock, targetInstanceDigest);
  const records = payload.split("\n").filter(line => line.length > 0);
  if (records.length === 0) throw new Error("private provider grant payload is empty");
  for (const line of records) {
    let document;
    try {
      document = JSON.parse(line);
    } catch {
      channel.clear();
      throw new Error("private provider grant payload is invalid");
    }
    if (!exactKeys(document, ["schema", "binding", "secret_base64"])
        || document.schema !== "workbench.runtime.provider_grant_private.v1"
        || !exactKeys(document.binding, [
          "command_id", "expires_at", "grant_digest", "grant_id", "model",
          "provider_ref", "run_id", "step_id", "target_instance_digest", "term_id",
        ])
        || typeof document.secret_base64 !== "string"
        || document.secret_base64.length === 0
        || !/^[A-Za-z0-9+/]+={0,2}$/.test(document.secret_base64)) {
      channel.clear();
      throw new Error("private provider grant payload is invalid");
    }
    const secret = Buffer.from(document.secret_base64, "base64");
    try {
      channel.accept(document.binding, secret);
    } finally {
      secret.fill(0);
    }
  }
  return channel;
}

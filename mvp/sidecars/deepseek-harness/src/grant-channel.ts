import { createHash } from "node:crypto";
import { Socket } from "node:net";


const DIGEST = /^[0-9a-f]{64}$/;
const BINDING_IDENTITY_FIELDS = Object.freeze([
  "command_id", "run_id", "term_id", "step_id", "provider_ref",
]);
const GRANT_HEADER_FIELDS = Object.freeze(["schema", "binding", "grant_digest"]);
const GRANT_BINDING_FIELDS = Object.freeze([
  "grant_id", "target", "session_id", "command_id", "run_id", "term_id", "step_id",
  "provider_id", "provider_profile_digest", "model", "scopes", "issued_at", "expires_at",
  "grant_nonce_digest",
]);
const GRANT_TARGET_FIELDS = Object.freeze([
  "runtime_id", "build_id", "lease_id", "instance_id_digest", "instance_nonce_digest",
  "host_generation", "lease_generation_seq", "expires_at",
]);
const GRANT_MAGIC = Buffer.from("JAGTGRN1", "ascii");
const ACK_MAGIC = Buffer.from("JAGTACK1", "ascii");
const WIRE_VERSION = 1;
const GRANT_PREFIX_BYTES = 17;
const MAX_HEADER_BYTES = 65_536;
const MAX_SECRET_BYTES = 65_536;
const MAX_ACK_BYTES = 8_192;


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


function canonicalDigest(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
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
        resolved_model: entry.binding.model,
        acknowledged_at: this.clock(),
      }),
    });
  }

  clear() {
    for (const { secret } of this.grants.values()) secret.fill(0);
    this.grants.clear();
  }

  static digest(binding) {
    return canonicalDigest(binding);
  }
}


function validateFormalGrant(document, targetIdentity, clock) {
  if (!exactKeys(document, GRANT_HEADER_FIELDS)
      || document.schema !== "workbench.runtime.provider_grant_private.v1"
      || !DIGEST.test(document.grant_digest)
      || !exactKeys(document.binding, GRANT_BINDING_FIELDS)
      || !exactKeys(document.binding.target, GRANT_TARGET_FIELDS)
      || canonicalDigest(document.binding) !== document.grant_digest) {
    throw new Error("private provider grant header is invalid");
  }
  const binding = document.binding;
  const target = binding.target;
  if (target.runtime_id !== targetIdentity.runtimeId
      || target.build_id !== targetIdentity.buildId
      || !DIGEST.test(target.instance_id_digest)
      || !DIGEST.test(target.instance_nonce_digest)
      || !Number.isSafeInteger(target.lease_generation_seq)
      || target.lease_generation_seq < 1
      || !Number.isFinite(target.expires_at)
      || typeof binding.grant_id !== "string" || binding.grant_id.length === 0
      || typeof binding.provider_id !== "string" || binding.provider_id.length === 0
      || binding.provider_id.includes(":") || binding.provider_id.includes("/")
      || !DIGEST.test(binding.provider_profile_digest)
      || !DIGEST.test(binding.grant_nonce_digest)
      || typeof binding.model !== "string" || binding.model.length === 0
      || !Array.isArray(binding.scopes) || binding.scopes.length === 0
      || binding.scopes.some(scope => typeof scope !== "string" || scope.length === 0)
      || new Set(binding.scopes).size !== binding.scopes.length
      || !Number.isFinite(binding.issued_at)
      || !Number.isFinite(binding.expires_at)
      || binding.expires_at <= clock()
      || binding.expires_at > target.expires_at
      || ["session_id", "command_id", "run_id", "term_id", "step_id"].some(
        field => typeof binding[field] !== "string" || binding[field].length === 0,
      )) {
    throw new Error("private provider grant binding is invalid or expired");
  }
  return binding;
}


function ackFrame(binding, grantDigest) {
  const payload = Buffer.from(JSON.stringify({
    schema: "workbench.runtime.provider_grant_ack.v1",
    grant_id: binding.grant_id,
    grant_digest: grantDigest,
    target_instance_digest: binding.target.instance_id_digest,
  }), "utf8");
  if (payload.length > MAX_ACK_BYTES) {
    throw new Error("private provider grant acknowledgement is too large");
  }
  const prefix = Buffer.alloc(13);
  ACK_MAGIC.copy(prefix, 0);
  prefix.writeUInt8(WIRE_VERSION, 8);
  prefix.writeUInt32BE(payload.length, 9);
  return Buffer.concat([prefix, payload]);
}


export function startPreopenedGrantReceiver(
  fd,
  targetIdentity,
  clock = () => Date.now() / 1000,
) {
  if (!Number.isSafeInteger(fd) || fd <= 2
      || targetIdentity === null || typeof targetIdentity !== "object"
      || typeof targetIdentity.runtimeId !== "string" || targetIdentity.runtimeId.length === 0
      || typeof targetIdentity.buildId !== "string" || targetIdentity.buildId.length === 0) {
    throw new Error("private provider grant descriptor is invalid");
  }
  const channel = new EphemeralGrantChannel(clock);
  let endpoint;
  try {
    endpoint = new Socket({ fd, readable: true, writable: true });
  } catch {
    throw new Error("private provider grant descriptor is unavailable");
  }
  let buffered = Buffer.alloc(0);
  let expectedBytes = null;
  const ready = new Promise((resolve, reject) => {
    const fail = message => {
      channel.clear();
      buffered.fill(0);
      endpoint.destroy();
      reject(new Error(message));
    };
    endpoint.on("error", () => fail("private provider grant descriptor is unavailable"));
    endpoint.on("end", () => {
      if (expectedBytes === null || buffered.length !== expectedBytes) {
        fail("private provider grant payload is incomplete");
      }
    });
    endpoint.on("data", chunk => {
      buffered = Buffer.concat([buffered, chunk]);
      if (buffered.length >= GRANT_PREFIX_BYTES && expectedBytes === null) {
        if (!buffered.subarray(0, 8).equals(GRANT_MAGIC)
            || buffered.readUInt8(8) !== WIRE_VERSION) {
          fail("private provider grant framing is invalid");
          return;
        }
        const headerBytes = buffered.readUInt32BE(9);
        const secretBytes = buffered.readUInt32BE(13);
        if (headerBytes === 0 || headerBytes > MAX_HEADER_BYTES
            || secretBytes === 0 || secretBytes > MAX_SECRET_BYTES) {
          fail("private provider grant framing is invalid");
          return;
        }
        expectedBytes = GRANT_PREFIX_BYTES + headerBytes + secretBytes;
      }
      if (expectedBytes === null || buffered.length < expectedBytes) return;
      if (buffered.length !== expectedBytes) {
        fail("private provider grant contains trailing bytes");
        return;
      }
      const headerBytes = buffered.readUInt32BE(9);
      let document;
      try {
        document = JSON.parse(
          buffered.subarray(GRANT_PREFIX_BYTES, GRANT_PREFIX_BYTES + headerBytes).toString("utf8"),
        );
      } catch {
        fail("private provider grant header is invalid");
        return;
      }
      let binding;
      try {
        binding = validateFormalGrant(document, targetIdentity, clock);
      } catch (error) {
        fail(error instanceof Error ? error.message : "private provider grant is invalid");
        return;
      }
      const secret = buffered.subarray(GRANT_PREFIX_BYTES + headerBytes);
      let acknowledgement;
      try {
        acknowledgement = channel.accept({
          grant_id: binding.grant_id,
          grant_digest: document.grant_digest,
          target_instance_digest: binding.target.instance_id_digest,
          command_id: binding.command_id,
          run_id: binding.run_id,
          term_id: binding.term_id,
          step_id: binding.step_id,
          provider_ref: `provider-profile:${binding.provider_id}`,
          model: binding.model,
          expires_at: binding.expires_at,
        }, secret);
        const frame = ackFrame(binding, acknowledgement.grant_digest);
        buffered.fill(0);
        endpoint.end(frame, () => {
          endpoint.destroy();
          resolve(channel);
        });
      } catch {
        secret.fill(0);
        fail("private provider grant payload is invalid");
      }
    });
  });
  // Mark the background failure as observed; query.start still awaits the same
  // promise and reports the closed failure through Host v2.
  ready.catch(() => {});
  return Object.freeze({ channel, ready });
}


export function readPreopenedGrantChannel(fd, targetIdentity, clock) {
  const receiver = startPreopenedGrantReceiver(fd, targetIdentity, clock);
  return receiver;
}

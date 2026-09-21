import { createHash } from 'node:crypto';
import { realpathSync, statSync } from 'node:fs';

const PROTOCOL = 'reservation-execution-confirmation';
const terminal = new Set(['COMMITTED', 'ABORTED', 'UNKNOWN']);
function fail(code, message) { throw Object.assign(new Error(message), { code }); }
function text(value, name) {
  if (typeof value !== 'string' || !value.trim() || value.length > 4096) fail('EFFECT_INVALID_INPUT', `Invalid ${name}`);
  return value;
}
function scopeOf(value) {
  if (!value || typeof value !== 'object') fail('EFFECT_INVALID_SCOPE', 'Scope required');
  return Object.fromEntries(['projectId', 'sessionId', 'agentId'].map(key => [key, text(value[key], key)]));
}
function canonical(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (!value || typeof value !== 'object' || seen.has(value)) fail('EFFECT_INVALID_INPUT', 'Expected finite, acyclic JSON');
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      if (Object.keys(value).length !== value.length) fail('EFFECT_INVALID_INPUT', 'Sparse arrays are not JSON');
      return value.map(item => canonical(item, seen));
    }
    if (![Object.prototype, null].includes(Object.getPrototypeOf(value))) fail('EFFECT_INVALID_INPUT', 'Expected plain JSON object');
    const result = Object.create(null);
    for (const key of Object.keys(value).sort()) result[key] = canonical(value[key], seen);
    return result;
  } finally { seen.delete(value); }
}
function json(value) {
  const result = JSON.stringify(canonical(value));
  if (Buffer.byteLength(result) > 1024 * 1024) fail('EFFECT_INVALID_INPUT', 'JSON exceeds 1 MiB');
  return result;
}
function time(value, fallback) {
  if (value === undefined) return fallback;
  if (!Number.isSafeInteger(value) || value < 0) fail('EFFECT_INVALID_INPUT', 'Expected nonnegative integer time');
  return value;
}
function hash(value) { return createHash('sha256').update(json(value)).digest('hex'); }
function sameScope(a, b) { return ['projectId', 'sessionId', 'agentId'].every(key => a[key] === b[key]); }

/** Single-machine durable ledger, NOT an external exactly-once or participant-2PC implementation. */
export class EffectStore {
  constructor(memoryStore) {
    this.db = memoryStore.db; // MemoryStore remains the sole lifecycle owner.
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS effect_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
      INSERT OR IGNORE INTO effect_schema VALUES (1, 1);
    `);
    if (this.db.prepare('SELECT version FROM effect_schema WHERE id=1').get().version !== 1) {
      fail('EFFECT_SCHEMA_UNSUPPORTED', 'Unsupported effect schema');
    }
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS effect_identities (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, visibility TEXT NOT NULL, fingerprint TEXT NOT NULL,
        identity_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS effect_events (
        id TEXT NOT NULL REFERENCES effect_identities(id), version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('PREPARED','EXECUTING','COMMITTED','ABORTED','UNKNOWN')),
        valid_time INTEGER NOT NULL, system_time INTEGER NOT NULL, record_json TEXT NOT NULL,
        PRIMARY KEY(id, version)
      );
      CREATE INDEX IF NOT EXISTS effect_scope_idx ON effect_identities(project_id,session_id,agent_id);
      CREATE INDEX IF NOT EXISTS effect_time_idx ON effect_events(id,valid_time,system_time);
      CREATE TRIGGER IF NOT EXISTS effect_events_no_update BEFORE UPDATE ON effect_events
        BEGIN SELECT RAISE(ABORT, 'effect events are append-only'); END;
      CREATE TRIGGER IF NOT EXISTS effect_events_no_delete BEFORE DELETE ON effect_events
        BEGIN SELECT RAISE(ABORT, 'effect events are append-only'); END;
      CREATE TRIGGER IF NOT EXISTS effect_identity_no_update BEFORE UPDATE ON effect_identities
        BEGIN SELECT RAISE(ABORT, 'effect identity is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS effect_identity_no_delete BEFORE DELETE ON effect_identities
        BEGIN SELECT RAISE(ABORT, 'effect identity is immutable'); END;
    `);
  }

  prepare(input) {
    const scope = scopeOf(input);
    const callId = text(input.callId, 'callId');
    const id = `effect:${hash([scope.projectId, scope.sessionId, callId])}`;
    if (!['private', 'project'].includes(input.visibility)) fail('EFFECT_INVALID_INPUT', 'visibility required');
    if ((input.protocol ?? PROTOCOL) !== PROTOCOL) fail('EFFECT_PROTOCOL_UNSUPPORTED', 'No participant prepare/commit/abort adapter is implemented');
    let workspaceRoot;
    try {
      workspaceRoot = realpathSync.native(text(input.workspaceRoot, 'workspaceRoot'));
      if (!statSync(workspaceRoot).isDirectory()) throw new Error('not directory');
    } catch { fail('EFFECT_INVALID_INPUT', 'workspaceRoot must be an existing directory'); }
    if (!input.sandboxPolicy || typeof input.sandboxPolicy !== 'object' || Array.isArray(input.sandboxPolicy)) fail('EFFECT_INVALID_INPUT', 'sandboxPolicy required');
    const identity = JSON.parse(json({ ...scope, visibility: input.visibility, callId,
      stepId: text(input.stepId, 'stepId'), toolName: text(input.toolName, 'toolName'),
      input: input.input, workspaceRoot, sandboxPolicy: input.sandboxPolicy, protocol: PROTOCOL,
      causationId: input.causationId === undefined ? null : text(input.causationId, 'causationId'),
      parentId: input.parentId === undefined ? null : text(input.parentId, 'parentId'),
      correlationId: input.correlationId === undefined ? callId : text(input.correlationId, 'correlationId'),
    }));
    const fingerprint = hash(identity);
    const validTime = time(input.validTime, Date.now());
    return this.#transaction(() => {
      const existing = this.db.prepare('SELECT fingerprint FROM effect_identities WHERE id=?').get(id);
      if (existing) {
        if (existing.fingerprint !== fingerprint) fail('EFFECT_ID_CONFLICT', 'Effect identity is bound to different input or policy');
        return this.#current(id, scope, true);
      }
      for (const cause of [identity.parentId, identity.causationId].filter(Boolean)) {
        try { this.#current(cause, scope); } catch { fail('EFFECT_CAUSE_NOT_FOUND', 'Cause must identify a visible durable effect'); }
      }
      this.db.prepare('INSERT INTO effect_identities VALUES (?,?,?,?,?,?,?)').run(
        id, scope.projectId, scope.sessionId, scope.agentId, input.visibility, fingerprint, json(identity));
      return this.#append({ ...identity, id, fingerprint, version: 0, owner: null, state: 'PREPARED', result: null, evidence: null }, { validTime });
    });
  }

  begin(effectId, owner, { validTime } = {}) {
    const normalized = this.#owner(owner);
    return this.#transaction(() => {
      const current = this.#current(effectId, normalized.scope, true);
      if (current.state !== 'PREPARED') fail('EFFECT_STATE_CONFLICT', 'Only PREPARED can be dispatched; never retry UNKNOWN');
      return this.#append(current, { state: 'EXECUTING', owner: normalized.id, validTime });
    });
  }

  finish(effectId, owner, { state, result = null, evidence = null, validTime } = {}) {
    const normalized = this.#owner(owner);
    if (!terminal.has(state)) fail('EFFECT_INVALID_STATE', 'Expected COMMITTED, ABORTED or UNKNOWN');
    return this.#transaction(() => {
      const current = this.#current(effectId, normalized.scope, true);
      if (current.state !== 'EXECUTING') fail('EFFECT_STATE_CONFLICT', 'Only active execution can finish');
      if (current.owner !== normalized.id) fail('EFFECT_OWNER_CONFLICT', 'Execution belongs to another owner');
      return this.#append(current, { state, result, evidence, validTime });
    });
  }

  /** Call only during exclusive cold-start recovery, before accepting new dispatches. */
  recover(scope) {
    const normalized = scopeOf(scope);
    return this.#transaction(() => {
      const rows = this.db.prepare('SELECT id FROM effect_identities WHERE project_id=? AND session_id=? AND agent_id=?')
        .all(normalized.projectId, normalized.sessionId, normalized.agentId);
      return rows.map(({ id }) => {
        const current = this.#current(id, normalized, true);
        return current.state === 'EXECUTING'
          ? this.#append(current, { state: 'UNKNOWN', evidence: { reason: 'recovery-interrupted-dispatch' } }) : current;
      });
    });
  }

  list(scope, { validAt, systemAt, limit = 100 } = {}) {
    const s = scopeOf(scope);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1000) fail('EFFECT_INVALID_INPUT', 'limit must be 1..1000');
    return this.db.prepare(`WITH eligible AS (
      SELECT e.*, ROW_NUMBER() OVER(PARTITION BY e.id ORDER BY e.valid_time DESC,e.system_time DESC,e.version DESC) AS rank
      FROM effect_events e JOIN effect_identities i ON i.id=e.id
      WHERE i.project_id=? AND (i.visibility='project' OR (i.session_id=? AND i.agent_id=?))
      AND e.valid_time<=? AND e.system_time<=?
      AND NOT EXISTS (
        SELECT 1 FROM effect_events correction WHERE correction.id=e.id
        AND json_extract(correction.record_json,'$.supersedesVersion')=e.version
        AND correction.valid_time<=? AND correction.system_time<=?))
      SELECT record_json FROM eligible WHERE rank=1 ORDER BY system_time DESC,id LIMIT ?`)
      .all(s.projectId, s.sessionId, s.agentId, time(validAt, Number.MAX_SAFE_INTEGER), time(systemAt, Number.MAX_SAFE_INTEGER),
        time(validAt, Number.MAX_SAFE_INTEGER), time(systemAt, Number.MAX_SAFE_INTEGER), limit)
      .map(row => JSON.parse(row.record_json));
  }

  graph(scope, filters = {}) {
    const nodes = this.list(scope, filters);
    const visible = new Set(nodes.map(node => node.id));
    const edges = [];
    for (const node of nodes) {
      for (const [field, relation] of [['parentId', 'parent'], ['causationId', 'causation']]) {
        if (visible.has(node[field])) edges.push({ from: node[field], to: node.id, relation });
      }
    }
    return { nodes, edges };
  }

  /** Trusted adapter boundary: never construct readback callbacks from model/HTTP payloads. */
  reconcile(effectId, proof) {
    const scope = scopeOf(proof?.scope);
    const current = this.#current(effectId, scope, true);
    if (current.state !== 'UNKNOWN') fail('EFFECT_STATE_CONFLICT', 'Only UNKNOWN can be reconciled');
    if (proof.kind === 'manual') return current; // Human opinion is not machine evidence.
    if (proof.kind !== 'adapter-readback' || typeof proof.readback !== 'function') fail('EFFECT_PROOF_REQUIRED', 'Trusted read-only adapter required');
    text(proof.adapterId, 'adapterId');
    const observed = proof.readback(structuredClone(current));
    if (!observed || !['COMMITTED', 'ABORTED'].includes(observed.state)
      || !observed.evidence || typeof observed.evidence.receipt !== 'string' || !observed.evidence.receipt.trim()) {
      fail('EFFECT_PROOF_REQUIRED', 'Definitive synchronous readback receipt required');
    }
    return this.#transaction(() => {
      const latest = this.#current(effectId, scope, true);
      if (latest.version !== current.version) fail('EFFECT_STATE_CONFLICT', 'Effect changed during readback');
      const dispatchedAt = this.db.prepare("SELECT valid_time FROM effect_events WHERE id=? AND state='EXECUTING' ORDER BY version DESC LIMIT 1")
        .get(effectId).valid_time;
      const validTime = time(proof.validTime, Math.max(Date.now(), latest.validTime));
      if (validTime < dispatchedAt) fail('EFFECT_INVALID_TIME_ORDER', 'Completion cannot predate dispatch');
      return this.#append(latest, { state: observed.state, result: observed.result ?? null,
        evidence: { adapterId: proof.adapterId, ...observed.evidence }, validTime,
        supersedesVersion: latest.version });
    });
  }

  #owner(owner) { return { id: text(owner?.id, 'owner.id'), scope: scopeOf(owner?.scope) }; }
  #current(id, scope, write = false) {
    text(id, 'effectId');
    const row = this.db.prepare('SELECT record_json FROM effect_events WHERE id=? ORDER BY version DESC LIMIT 1').get(id);
    const record = row && JSON.parse(row.record_json);
    if (!record || record.projectId !== scope.projectId ||
      ((write || record.visibility === 'private') && !sameScope(record, scope))) fail('EFFECT_NOT_FOUND', 'Effect not found in scope');
    return record;
  }
  #transaction(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = fn(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  #append(current, change) {
    const validTime = time(change.validTime, Math.max(Date.now(), current.validTime ?? 0));
    // Ordinary state transitions are chronological. A verified correction can
    // replace UNKNOWN retroactively; its explicit supersession is as-of aware.
    if (change.supersedesVersion === undefined && validTime < (current.validTime ?? 0)) {
      fail('EFFECT_INVALID_TIME_ORDER', 'State transition cannot predate prior state');
    }
    const latest = this.db.prepare('SELECT MAX(system_time) AS t FROM effect_events').get().t;
    const systemTime = Math.max(Date.now(), (latest ?? -1) + 1);
    const record = JSON.parse(json({ ...current, ...change, version: current.version + 1, systemTime,
      validTime }));
    this.db.prepare('INSERT INTO effect_events VALUES (?,?,?,?,?,?)')
      .run(record.id, record.version, record.state, record.validTime, record.systemTime, json(record));
    return record;
  }
}

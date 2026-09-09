import { createHash, randomUUID } from 'node:crypto';
import { chmodSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import { DatabaseSync } from 'node:sqlite';

import { sanitizeEvent } from './record-sanitizer.mjs';

const SCHEMA_VERSION = 1;
const MEMORY_KINDS = new Set(['episodic', 'semantic', 'procedural']);
const VISIBILITIES = new Set(['private', 'project']);
const PROCEDURE_STATUSES = new Set(['candidate', 'confirmed']);
const MAX_LIST_LIMIT = 1_000;
const MAX_JSON_BYTES = 10 * 1024 * 1024;

function memoryError(code, message, ErrorClass = Error) {
  const error = new ErrorClass(message);
  error.code = code;
  return error;
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireText(value, name, code, maxLength = 4_096) {
  if (typeof value !== 'string' || value.trim().length === 0 || value.length > maxLength) {
    throw memoryError(code, `${name} must be a non-empty string`, TypeError);
  }
  return value;
}

function validateScope(scope) {
  if (!isPlainObject(scope)) throw memoryError('MEMORY_INVALID_SCOPE', 'scope must be an object', TypeError);
  return {
    projectId: requireText(scope.projectId, 'projectId', 'MEMORY_INVALID_SCOPE'),
    sessionId: requireText(scope.sessionId, 'sessionId', 'MEMORY_INVALID_SCOPE'),
    agentId: requireText(scope.agentId, 'agentId', 'MEMORY_INVALID_SCOPE'),
  };
}

function canonicalize(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw memoryError('MEMORY_INVALID_RECORD', 'value must be JSON serializable', TypeError);
    return value;
  }
  if (typeof value !== 'object') {
    throw memoryError('MEMORY_INVALID_RECORD', 'value must be JSON serializable', TypeError);
  }
  if (seen.has(value)) throw memoryError('MEMORY_INVALID_RECORD', 'value must not be cyclic', TypeError);
  seen.add(value);
  try {
    if (Array.isArray(value)) return value.map(item => canonicalize(item, seen));
    if (!isPlainObject(value)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'value must contain only JSON objects and arrays', TypeError);
    }
    const result = {};
    for (const key of Object.keys(value).sort()) result[key] = canonicalize(value[key], seen);
    return result;
  } finally {
    seen.delete(value);
  }
}

function serialize(value, code = 'MEMORY_INVALID_RECORD') {
  let json;
  try {
    json = JSON.stringify(canonicalize(value));
  } catch (error) {
    if (error.code === 'MEMORY_INVALID_RECORD' && code !== error.code) error.code = code;
    throw error;
  }
  if (Buffer.byteLength(json) > MAX_JSON_BYTES) {
    throw memoryError(code, 'JSON value is too large', RangeError);
  }
  return json;
}

function parseJson(json) {
  return JSON.parse(json);
}

function validateActor(actor) {
  if (!isPlainObject(actor)
      || typeof actor.id !== 'string'
      || actor.id.trim().length === 0
      || !['model', 'operator', 'system'].includes(actor.role)) {
    throw memoryError(
      'MEMORY_INVALID_ACTOR',
      'actor must be { id, role } with role model, operator, or system',
      TypeError,
    );
  }
  return { id: actor.id, role: actor.role };
}

function validateTimestamp(value, name, defaultValue) {
  if (value === undefined) return defaultValue;
  if (!Number.isSafeInteger(value) || value < 0) {
    throw memoryError('MEMORY_INVALID_RECORD', `${name} must be a non-negative safe integer`, TypeError);
  }
  return value;
}

function validateSourceRefs(sourceRefs) {
  if (!Array.isArray(sourceRefs)) {
    throw memoryError('MEMORY_INVALID_RECORD', 'sourceRefs must be an array', TypeError);
  }
  for (const source of sourceRefs) {
    if (!isPlainObject(source)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'sourceRefs must contain objects', TypeError);
    }
    if (Object.hasOwn(source, 'sessionId')) requireText(source.sessionId, 'source sessionId', 'MEMORY_INVALID_RECORD');
    if (Object.hasOwn(source, 'seq') && (!Number.isSafeInteger(source.seq) || source.seq < 0)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'source seq must be a non-negative safe integer', TypeError);
    }
  }
  return serialize(sourceRefs);
}

function validateSemanticContent(content) {
  if (!isPlainObject(content) || !Array.isArray(content.nodes) || !Array.isArray(content.edges)) {
    throw memoryError('MEMORY_INVALID_RECORD', 'semantic content must contain nodes and edges arrays', TypeError);
  }
  for (const node of content.nodes) {
    if (!isPlainObject(node)) throw memoryError('MEMORY_INVALID_RECORD', 'semantic nodes must be objects', TypeError);
    requireText(node.id, 'semantic node id', 'MEMORY_INVALID_RECORD');
    requireText(node.type, 'semantic node type', 'MEMORY_INVALID_RECORD');
  }
  for (const edge of content.edges) {
    if (!isPlainObject(edge)) throw memoryError('MEMORY_INVALID_RECORD', 'semantic edges must be objects', TypeError);
    requireText(edge.from, 'semantic edge from', 'MEMORY_INVALID_RECORD');
    requireText(edge.relation, 'semantic edge relation', 'MEMORY_INVALID_RECORD');
    requireText(edge.to, 'semantic edge to', 'MEMORY_INVALID_RECORD');
  }
}

function validateProceduralContent(content) {
  if (!isPlainObject(content)
      || !Array.isArray(content.rules)
      || content.rules.length === 0
      || content.rules.some(rule => typeof rule !== 'string' || rule.trim().length === 0)
      || !isPlainObject(content.applicability)) {
    throw memoryError(
      'MEMORY_INVALID_RECORD',
      'procedural content must contain non-empty rules and an applicability object',
      TypeError,
    );
  }
  if (Object.hasOwn(content, 'protected') && typeof content.protected !== 'boolean') {
    throw memoryError('MEMORY_INVALID_RECORD', 'procedural protected must be boolean', TypeError);
  }
}

function rowToRecord(row) {
  return {
    id: row.id,
    version: row.version,
    kind: row.kind,
    projectId: row.project_id,
    sessionId: row.session_id,
    agentId: row.agent_id,
    visibility: row.visibility,
    content: parseJson(row.content_json),
    summary: row.summary,
    sourceRefs: parseJson(row.source_refs_json),
    validTime: row.valid_time,
    systemTime: row.system_time,
    status: row.status,
    actor: parseJson(row.actor_json),
  };
}

function initializeSchema(db) {
  const version = db.prepare('PRAGMA user_version').get().user_version;
  if (version > SCHEMA_VERSION) {
    throw memoryError(
      'MEMORY_SCHEMA_UNSUPPORTED',
      `memory schema ${version} is newer than supported schema ${SCHEMA_VERSION}`,
    );
  }
  db.exec(`
    CREATE TABLE IF NOT EXISTS memory_records (
      project_id TEXT NOT NULL,
      id TEXT NOT NULL,
      version INTEGER NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('episodic', 'semantic', 'procedural')),
      session_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      visibility TEXT NOT NULL CHECK (visibility IN ('private', 'project')),
      content_json TEXT NOT NULL,
      summary TEXT NOT NULL,
      source_refs_json TEXT NOT NULL,
      valid_time INTEGER NOT NULL,
      system_time INTEGER NOT NULL,
      status TEXT NOT NULL,
      actor_json TEXT NOT NULL,
      PRIMARY KEY (project_id, id, version)
    );
    CREATE INDEX IF NOT EXISTS memory_records_scope_idx
      ON memory_records (project_id, kind, id, version DESC);
    CREATE INDEX IF NOT EXISTS memory_records_time_idx
      ON memory_records (project_id, system_time, valid_time);
    CREATE TABLE IF NOT EXISTS memory_event_sources (
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      project_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      event_hash TEXT NOT NULL,
      memory_id TEXT,
      ingested_at INTEGER NOT NULL,
      PRIMARY KEY (session_id, seq)
    );
    CREATE TABLE IF NOT EXISTS memory_cursors (
      session_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      agent_id TEXT NOT NULL,
      seq INTEGER NOT NULL
    );
  `);
  if (version < SCHEMA_VERSION) db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
}

export class MemoryStore {
  #closed = false;

  constructor(path) {
    requireText(path, 'path', 'MEMORY_INVALID_PATH', 32_768);
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    try {
      this.db.exec('PRAGMA journal_mode = WAL');
      this.db.exec('PRAGMA synchronous = FULL');
      this.db.exec('PRAGMA foreign_keys = ON');
      this.db.exec('PRAGMA busy_timeout = 5000');
      initializeSchema(this.db);
      chmodSync(path, 0o600);
    } catch (error) {
      this.db.close();
      this.#closed = true;
      throw error;
    }
  }

  close() {
    if (this.#closed) return;
    this.db.close();
    this.#closed = true;
  }

  ingestEvents({ projectId, sessionId, agentId, events } = {}) {
    this.#assertOpen();
    const scope = validateScope({ projectId, sessionId, agentId });
    if (!Array.isArray(events)) {
      throw memoryError('MEMORY_INVALID_EVENT', 'events must be an array', TypeError);
    }

    const preparedEvents = events.map(event => {
      let sanitized;
      try {
        sanitized = sanitizeEvent(event);
      } catch (error) {
        if (!error.code) error.code = 'MEMORY_INVALID_EVENT';
        throw error;
      }
      let canonical;
      try {
        canonical = serialize(event, 'MEMORY_INVALID_EVENT');
      } catch (error) {
        error.code = 'MEMORY_INVALID_EVENT';
        throw error;
      }
      return {
        source: event,
        sanitized,
        hash: createHash('sha256').update(canonical).digest('hex'),
      };
    });

    return this.#transaction(() => {
      const cursorRow = this.db.prepare(
        'SELECT project_id, agent_id, seq FROM memory_cursors WHERE session_id = ?',
      ).get(scope.sessionId);
      if (cursorRow
          && (cursorRow.project_id !== scope.projectId || cursorRow.agent_id !== scope.agentId)) {
        throw memoryError('MEMORY_SCOPE_CONFLICT', 'session is already bound to another project or agent');
      }
      let expected = (cursorRow?.seq ?? -1) + 1;
      let cursor = cursorRow?.seq ?? null;
      let inserted = 0;
      let skipped = 0;
      const sourceLookup = this.db.prepare(
        'SELECT project_id, agent_id, event_hash FROM memory_event_sources WHERE session_id = ? AND seq = ?',
      );
      const sourceInsert = this.db.prepare(`
        INSERT INTO memory_event_sources
          (session_id, seq, project_id, agent_id, event_hash, memory_id, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
      `);

      for (const item of preparedEvents) {
        const existing = sourceLookup.get(scope.sessionId, item.source.seq);
        if (existing) {
          if (existing.project_id !== scope.projectId
              || existing.agent_id !== scope.agentId
              || existing.event_hash !== item.hash) {
            throw memoryError('MEMORY_SOURCE_CONFLICT', 'event source already exists with different data');
          }
          skipped += 1;
          continue;
        }
        if (item.source.seq !== expected) {
          throw memoryError(
            'MEMORY_SEQUENCE_GAP',
            `expected event sequence ${expected}, received ${item.source.seq}`,
          );
        }

        const systemTime = this.#nextSystemTime();
        let memoryId = null;
        if (item.sanitized.event !== null) {
          memoryId = `event:${scope.sessionId}:${item.source.seq}`;
          const linkedSeqs = Array.isArray(item.source.sourceEventSeqs)
            ? item.source.sourceEventSeqs
            : [];
          const sourceRefs = [...new Set([...linkedSeqs, item.source.seq])]
            .sort((left, right) => left - right)
            .map(seq => ({
              projectId: scope.projectId,
              sessionId: scope.sessionId,
              seq,
            }));
          const content = {
            ...item.sanitized.event,
            redactions: item.sanitized.redactions,
          };
          this.#insertRecord({
            ...scope,
            id: memoryId,
            version: 1,
            kind: 'episodic',
            visibility: 'private',
            contentJson: serialize(content),
            summary: `Event ${item.source.seq}: ${item.source.type}`,
            sourceRefsJson: serialize(sourceRefs),
            validTime: validateTimestamp(item.source.time, 'event time', systemTime),
            systemTime,
            status: 'recorded',
            actorJson: serialize({ id: 'memory-ingest', role: 'system' }),
          });
          inserted += 1;
        } else {
          skipped += 1;
        }
        sourceInsert.run(
          scope.sessionId,
          item.source.seq,
          scope.projectId,
          scope.agentId,
          item.hash,
          memoryId,
          systemTime,
        );
        cursor = item.source.seq;
        expected += 1;
      }

      if (cursor !== null && cursor !== cursorRow?.seq) {
        this.db.prepare(`
          INSERT INTO memory_cursors (session_id, project_id, agent_id, seq)
          VALUES (?, ?, ?, ?)
          ON CONFLICT(session_id) DO UPDATE SET seq = excluded.seq
        `).run(scope.sessionId, scope.projectId, scope.agentId, cursor);
      }
      return { inserted, skipped, cursor };
    });
  }

  putMemory(record, { actor } = {}) {
    this.#assertOpen();
    if (!isPlainObject(record)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'record must be an object', TypeError);
    }
    const scope = validateScope(record);
    const normalizedActor = validateActor(actor);
    const id = record.id === undefined
      ? randomUUID()
      : requireText(record.id, 'id', 'MEMORY_INVALID_RECORD', 512);
    if (!MEMORY_KINDS.has(record.kind)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'kind must be episodic, semantic, or procedural', TypeError);
    }
    if (!VISIBILITIES.has(record.visibility)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'visibility must be private or project', TypeError);
    }
    const summary = requireText(record.summary, 'summary', 'MEMORY_INVALID_RECORD', 16_384);
    const validTime = validateTimestamp(record.validTime, 'validTime', Date.now());
    if (record.expectedVersion !== undefined
        && (!Number.isSafeInteger(record.expectedVersion) || record.expectedVersion < 0)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'expectedVersion must be a non-negative safe integer', TypeError);
    }
    if (record.kind === 'semantic') validateSemanticContent(record.content);
    if (record.kind === 'procedural') validateProceduralContent(record.content);
    const contentJson = serialize(record.content);
    const sourceRefsJson = validateSourceRefs(record.sourceRefs);
    const status = record.status ?? (record.kind === 'procedural' ? 'candidate' : 'active');
    if (record.kind === 'procedural' && !PROCEDURE_STATUSES.has(status)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'procedural status must be candidate or confirmed', TypeError);
    }
    if (record.kind !== 'procedural' && typeof status !== 'string') {
      throw memoryError('MEMORY_INVALID_RECORD', 'status must be a string', TypeError);
    }
    const protectedProcedure = record.kind === 'procedural' && record.content.protected === true;
    if ((status === 'confirmed' || protectedProcedure) && normalizedActor.role !== 'operator') {
      throw memoryError('MEMORY_OPERATOR_REQUIRED', 'only an operator can confirm or write protected procedures');
    }

    return this.#transaction(() => {
      const currentRow = this.db.prepare(`
        SELECT * FROM memory_records
        WHERE project_id = ? AND id = ?
        ORDER BY version DESC LIMIT 1
      `).get(scope.projectId, id);
      const current = currentRow ? rowToRecord(currentRow) : null;
      const currentVersion = current?.version ?? 0;
      if (record.expectedVersion !== undefined && record.expectedVersion !== currentVersion) {
        throw memoryError(
          'MEMORY_VERSION_CONFLICT',
          `expected version ${record.expectedVersion}, current version is ${currentVersion}`,
        );
      }
      if (current
          && (current.kind !== record.kind
            || current.sessionId !== scope.sessionId
            || current.agentId !== scope.agentId
            || current.visibility !== record.visibility)) {
        throw memoryError('MEMORY_LINEAGE_CONFLICT', 'memory identity, kind, owner, and visibility are immutable');
      }
      if (current?.kind === 'procedural'
          && current.content.protected === true
          && normalizedActor.role !== 'operator') {
        throw memoryError('MEMORY_OPERATOR_REQUIRED', 'only an operator can modify a protected procedure');
      }

      const version = currentVersion + 1;
      const systemTime = this.#nextSystemTime();
      this.#insertRecord({
        ...scope,
        id,
        version,
        kind: record.kind,
        visibility: record.visibility,
        contentJson,
        summary,
        sourceRefsJson,
        validTime,
        systemTime,
        status,
        actorJson: serialize(normalizedActor),
      });
      return this.get(scope, id, version);
    });
  }

  list(scope, filters = {}) {
    this.#assertOpen();
    const normalizedScope = validateScope(scope);
    if (!isPlainObject(filters)) {
      throw memoryError('MEMORY_INVALID_FILTER', 'filters must be an object', TypeError);
    }
    if (filters.kind !== undefined && !MEMORY_KINDS.has(filters.kind)) {
      throw memoryError('MEMORY_INVALID_FILTER', 'invalid memory kind', TypeError);
    }
    if (filters.visibility !== undefined && !VISIBILITIES.has(filters.visibility)) {
      throw memoryError('MEMORY_INVALID_FILTER', 'invalid visibility', TypeError);
    }
    if (filters.status !== undefined && typeof filters.status !== 'string') {
      throw memoryError('MEMORY_INVALID_FILTER', 'invalid status', TypeError);
    }
    const limit = filters.limit ?? 100;
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_LIST_LIMIT) {
      throw memoryError('MEMORY_INVALID_FILTER', `limit must be from 1 to ${MAX_LIST_LIMIT}`, RangeError);
    }

    const rows = this.db.prepare(`
      WITH visible AS (
        SELECT *, ROW_NUMBER() OVER (
          PARTITION BY project_id, id ORDER BY version DESC
        ) AS rank
        FROM memory_records
        WHERE project_id = ?
          AND (visibility = 'project' OR (session_id = ? AND agent_id = ?))
      )
      SELECT * FROM visible
      WHERE rank = 1
        AND (? IS NULL OR kind = ?)
        AND (? IS NULL OR visibility = ?)
        AND (? IS NULL OR status = ?)
      ORDER BY system_time DESC, id ASC
      LIMIT ?
    `).all(
      normalizedScope.projectId,
      normalizedScope.sessionId,
      normalizedScope.agentId,
      filters.kind ?? null,
      filters.kind ?? null,
      filters.visibility ?? null,
      filters.visibility ?? null,
      filters.status ?? null,
      filters.status ?? null,
      limit,
    );
    return rows.map(rowToRecord);
  }

  get(scope, id, version) {
    this.#assertOpen();
    const normalizedScope = validateScope(scope);
    requireText(id, 'id', 'MEMORY_INVALID_RECORD', 512);
    if (version !== undefined && (!Number.isSafeInteger(version) || version < 1)) {
      throw memoryError('MEMORY_INVALID_RECORD', 'version must be a positive safe integer', TypeError);
    }
    const row = version === undefined
      ? this.db.prepare(`
          SELECT * FROM memory_records
          WHERE project_id = ? AND id = ?
            AND (visibility = 'project' OR (session_id = ? AND agent_id = ?))
          ORDER BY version DESC LIMIT 1
        `).get(normalizedScope.projectId, id, normalizedScope.sessionId, normalizedScope.agentId)
      : this.db.prepare(`
          SELECT * FROM memory_records
          WHERE project_id = ? AND id = ? AND version = ?
            AND (visibility = 'project' OR (session_id = ? AND agent_id = ?))
        `).get(normalizedScope.projectId, id, version, normalizedScope.sessionId, normalizedScope.agentId);
    return row ? rowToRecord(row) : null;
  }

  graph(scope, { validAt, systemAt } = {}) {
    this.#assertOpen();
    const normalizedScope = validateScope(scope);
    const normalizedValidAt = validateTimestamp(validAt, 'validAt', Number.MAX_SAFE_INTEGER);
    const normalizedSystemAt = validateTimestamp(systemAt, 'systemAt', Number.MAX_SAFE_INTEGER);
    const rows = this.db.prepare(`
      WITH eligible AS (
        SELECT *, ROW_NUMBER() OVER (
          PARTITION BY project_id, id ORDER BY system_time DESC, version DESC
        ) AS rank
        FROM memory_records
        WHERE project_id = ? AND kind = 'semantic'
          AND valid_time <= ? AND system_time <= ?
          AND (visibility = 'project' OR (session_id = ? AND agent_id = ?))
      )
      SELECT * FROM eligible WHERE rank = 1
      ORDER BY id ASC
    `).all(
      normalizedScope.projectId,
      normalizedValidAt,
      normalizedSystemAt,
      normalizedScope.sessionId,
      normalizedScope.agentId,
    );
    const records = rows.map(rowToRecord);
    const nodesById = new Map();
    const edges = [];
    for (const record of records) {
      for (const node of record.content.nodes) nodesById.set(node.id, structuredClone(node));
      for (const edge of record.content.edges) {
        edges.push({ ...structuredClone(edge), memoryId: record.id, version: record.version });
      }
    }
    return { nodes: [...nodesById.values()], edges, records };
  }

  cursor(sessionId) {
    this.#assertOpen();
    requireText(sessionId, 'sessionId', 'MEMORY_INVALID_SCOPE');
    return this.db.prepare('SELECT seq FROM memory_cursors WHERE session_id = ?').get(sessionId)?.seq ?? null;
  }

  #assertOpen() {
    if (this.#closed) throw memoryError('MEMORY_STORE_CLOSED', 'memory store is closed');
  }

  #transaction(operation) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = operation();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }

  #nextSystemTime() {
    const latest = this.db.prepare('SELECT MAX(system_time) AS value FROM memory_records').get().value;
    return Math.max(Date.now(), (latest ?? -1) + 1);
  }

  #insertRecord(record) {
    this.db.prepare(`
      INSERT INTO memory_records (
        project_id, id, version, kind, session_id, agent_id, visibility,
        content_json, summary, source_refs_json, valid_time, system_time,
        status, actor_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      record.projectId,
      record.id,
      record.version,
      record.kind,
      record.sessionId,
      record.agentId,
      record.visibility,
      record.contentJson,
      record.summary,
      record.sourceRefsJson,
      record.validTime,
      record.systemTime,
      record.status,
      record.actorJson,
    );
  }
}

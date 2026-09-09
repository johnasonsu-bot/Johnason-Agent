import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { DatabaseSync } from 'node:sqlite';

import { MemoryStore } from '../src/memory-store.mjs';
import { sanitizeEvent } from '../src/record-sanitizer.mjs';

const ownScope = Object.freeze({ projectId: 'project-a', sessionId: 'session-a', agentId: 'agent-a' });
const operator = Object.freeze({ actor: { id: 'local-user', role: 'operator' } });
const model = Object.freeze({ actor: { id: 'configured-model', role: 'model' } });

async function memoryPath(name) {
  const directory = await mkdtemp(join(tmpdir(), `dsh-memory-${name}-`));
  return join(directory, 'memory.sqlite');
}

function memory(overrides = {}) {
  return {
    kind: 'semantic',
    ...ownScope,
    visibility: 'project',
    content: {
      nodes: [{ id: 'service', type: 'component', label: 'Billing API' }],
      edges: [],
    },
    summary: 'Billing API architecture',
    sourceRefs: [{
      type: 'operator',
      operatorId: 'local-user',
      reason: 'unit-test definition',
    }],
    ...overrides,
  };
}

test('persists episodic, semantic, and procedural memories across restart as detached JSON records', async () => {
  const path = await memoryPath('restart');
  let store = new MemoryStore(path);
  assert.equal(store.db.prepare('PRAGMA journal_mode').get().journal_mode, 'wal');
  assert.equal(store.db.prepare('PRAGMA synchronous').get().synchronous, 2);

  assert.deepEqual(store.ingestEvents({
    ...ownScope,
    events: [{ seq: 0, type: 'tool/result', data: { value: 'needle' }, time: 1 }],
  }), { inserted: 1, skipped: 0, cursor: 0 });

  const semanticContent = {
    nodes: [{ id: 'queue', type: 'component', label: 'Queue' }],
    edges: [{ from: 'queue', relation: 'feeds', to: 'worker' }],
  };
  const semantic = store.putMemory(memory({ id: 'architecture', content: semanticContent }), operator);
  const procedural = store.putMemory(memory({
    id: 'retry-rule',
    kind: 'procedural',
    content: { rules: ['Retry only idempotent requests'], applicability: { tool: 'http' } },
    summary: 'Safe retry candidate',
    status: 'candidate',
    sourceRefs: [{
      type: 'event',
      projectId: 'project-a',
      sessionId: 'session-a',
      agentId: 'agent-a',
      seq: 0,
    }],
  }), model);

  semanticContent.nodes[0].label = 'mutated by caller';
  semantic.content.nodes[0].label = 'mutated return';
  procedural.content.rules.push('mutated return');
  store.close();

  store = new MemoryStore(path);
  assert.equal(store.cursor('session-a'), 0);
  assert.equal(store.list(ownScope, { kind: 'episodic' }).length, 1);
  assert.equal(store.list(ownScope, { kind: 'semantic' }).length, 1);
  assert.equal(store.list(ownScope, { kind: 'procedural' }).length, 1);
  assert.equal(store.get(ownScope, 'architecture').content.nodes[0].label, 'Queue');
  assert.deepEqual(store.get(ownScope, 'retry-rule').content.rules, ['Retry only idempotent requests']);
  store.close();
});

test('deduplicates durable event sources across restart and never advances a cursor for an invalid batch', async () => {
  const path = await memoryPath('cursor');
  let store = new MemoryStore(path);
  const first = { seq: 0, type: 'tool/result', data: { value: 'one' }, time: 1 };

  assert.deepEqual(store.ingestEvents({ ...ownScope, events: [first] }), {
    inserted: 1,
    skipped: 0,
    cursor: 0,
  });
  store.close();
  store = new MemoryStore(path);
  assert.deepEqual(store.ingestEvents({ ...ownScope, events: [first] }), {
    inserted: 0,
    skipped: 1,
    cursor: 0,
  });
  assert.equal(store.list(ownScope, { kind: 'episodic' }).length, 1);

  assert.throws(
    () => store.ingestEvents({ ...ownScope, events: [{ ...first, data: { value: 'changed' } }] }),
    { code: 'MEMORY_SOURCE_CONFLICT' },
  );
  assert.throws(
    () => store.ingestEvents({ ...ownScope, events: [{ seq: 2, type: 'tool/result', data: {}, time: 2 }] }),
    { code: 'MEMORY_SEQUENCE_GAP' },
  );
  assert.throws(
    () => store.ingestEvents({
      ...ownScope,
      events: [{ seq: 1, type: 'tool/result', data: {}, time: 'not-a-dsh-timestamp' }],
    }),
    { code: 'MEMORY_INVALID_EVENT' },
  );
  assert.throws(
    () => store.ingestEvents({
      ...ownScope,
      events: [
        { seq: 1, type: 'tool/result', data: { value: 'valid' }, time: 2 },
        { seq: '2', type: 'tool/result', data: {}, time: 3 },
      ],
    }),
    { code: 'MEMORY_INVALID_EVENT' },
  );
  assert.equal(store.cursor('session-a'), 0);
  assert.equal(store.list(ownScope, { kind: 'episodic' }).length, 1);

  assert.deepEqual(store.ingestEvents({
    ...ownScope,
    events: [{ seq: 1, type: 'tool/result', data: { value: 'valid' }, time: 2 }],
  }), { inserted: 1, skipped: 0, cursor: 1 });
  store.close();
});

test('skips credential-domain events and explicitly records recognized redactions without changing ordinary shape', async () => {
  const store = new MemoryStore(await memoryPath('sanitize'));

  assert.deepEqual(store.ingestEvents({
    ...ownScope,
    events: [{ seq: 0, type: 'vault/credentials', data: { note: 'do not ingest' }, time: 1 }],
  }), { inserted: 0, skipped: 1, cursor: 0 });

  store.ingestEvents({
    ...ownScope,
    events: [{
      seq: 1,
      type: 'tool/result',
      time: 2,
      data: {
        password: 'synthetic-password',
        nested: { apiKey: 'sk-synthetic01234567890123456789', note: 'ordinary value' },
        output: 'Authorization: Bearer synthetic-token-value',
      },
    }],
  });

  const [record] = store.list(ownScope, { kind: 'episodic' });
  assert.equal(record.content.data.password, '[REDACTED]');
  assert.equal(record.content.data.nested.apiKey, '[REDACTED]');
  assert.equal(record.content.data.nested.note, 'ordinary value');
  assert.equal(record.content.data.output, '[REDACTED]');
  assert.deepEqual(record.content.redactions, [
    'data.password',
    'data.nested.apiKey',
    'data.output',
  ]);
  assert.deepEqual(sanitizeEvent({ seq: 7, type: 'log', time: 3, data: { count: 2, ok: true } }), {
    event: { seq: 7, type: 'log', time: 3, data: { count: 2, ok: true } },
    redactions: [],
  });
  store.close();
});

test('appends semantic revisions and reconstructs nodes and typed edges as of valid and system time', async () => {
  const store = new MemoryStore(await memoryPath('as-of'));
  const first = store.putMemory(memory({
    id: 'contract',
    validTime: 10,
    content: {
      nodes: [
        { id: 'api', type: 'service', label: 'API v1' },
        { id: 'db', type: 'database', label: 'Database' },
      ],
      edges: [{ from: 'api', relation: 'reads', to: 'db' }],
    },
  }), operator);
  const second = store.putMemory(memory({
    id: 'contract',
    expectedVersion: 1,
    validTime: 20,
    content: {
      nodes: [
        { id: 'api', type: 'service', label: 'API v2' },
        { id: 'db', type: 'database', label: 'Database' },
      ],
      edges: [{ from: 'api', relation: 'writes', to: 'db' }],
    },
  }), operator);

  assert.equal(first.version, 1);
  assert.equal(second.version, 2);
  assert.ok(second.systemTime > first.systemTime);
  assert.throws(
    () => store.putMemory(memory({ id: 'contract', expectedVersion: 1 }), operator),
    { code: 'MEMORY_VERSION_CONFLICT' },
  );
  assert.equal(store.get(ownScope, 'contract', 1).content.nodes[0].label, 'API v1');

  assert.deepEqual(store.graph(ownScope, { validAt: 15 }).edges, [
    { from: 'api', relation: 'reads', to: 'db', memoryId: 'contract', version: 1 },
  ]);
  assert.equal(store.graph(ownScope, { systemAt: first.systemTime }).records[0].version, 1);
  assert.equal(store.graph(ownScope, {}).nodes.find(node => node.id === 'api').label, 'API v2');

  const correction = store.putMemory(memory({
    id: 'contract',
    expectedVersion: 2,
    validTime: 10,
    content: {
      nodes: [
        { id: 'api', type: 'service', label: 'API v1 corrected' },
        { id: 'db', type: 'database', label: 'Database' },
      ],
      edges: [{ from: 'api', relation: 'reads-corrected', to: 'db' }],
    },
  }), operator);
  assert.equal(correction.version, 3);
  assert.equal(store.graph(ownScope, { validAt: 25 }).records[0].version, 2);
  assert.equal(store.graph(ownScope, {
    validAt: 15,
    systemAt: second.systemTime,
  }).records[0].version, 1);
  assert.equal(store.graph(ownScope, { validAt: 15 }).records[0].version, 3);
  store.close();
});

test('enforces project visibility and exact session plus agent ownership for private records', async () => {
  const store = new MemoryStore(await memoryPath('visibility'));
  store.putMemory(memory({ id: 'shared', visibility: 'project' }), operator);
  store.putMemory(memory({ id: 'private', visibility: 'private' }), operator);

  const sameProjectOtherOwner = { projectId: 'project-a', sessionId: 'session-b', agentId: 'agent-b' };
  assert.deepEqual(store.list(sameProjectOtherOwner, {}).map(record => record.id), ['shared']);
  assert.equal(store.get(sameProjectOtherOwner, 'private'), null);
  assert.deepEqual(store.list({ projectId: 'project-b', sessionId: 'session-a', agentId: 'agent-a' }, {}), []);
  assert.equal(store.get(ownScope, 'private').id, 'private');
  store.close();
});

test('allows models to propose candidates but only operators can confirm or modify protected procedures', async () => {
  const store = new MemoryStore(await memoryPath('procedure'));
  store.ingestEvents({
    ...ownScope,
    events: [{ seq: 0, type: 'tool/result', time: 1, data: { failure: 'source' } }],
  });
  const candidate = store.putMemory(memory({
    id: 'candidate',
    kind: 'procedural',
    status: 'candidate',
    content: { rules: ['Check exit status'], applicability: { tool: 'shell' } },
    sourceRefs: [{
      type: 'event',
      projectId: 'project-a',
      sessionId: 'session-a',
      agentId: 'agent-a',
      seq: 0,
    }],
  }), model);
  assert.equal(candidate.status, 'candidate');

  assert.throws(() => store.putMemory(memory({
    id: 'self-approved',
    kind: 'procedural',
    status: 'confirmed',
    content: { rules: ['Trust output'], applicability: {} },
  }), model), { code: 'MEMORY_OPERATOR_REQUIRED' });
  assert.throws(() => store.putMemory(memory({
    id: 'model-protected',
    kind: 'procedural',
    status: 'candidate',
    content: { protected: true, rules: ['System invariant'], applicability: {} },
  }), model), { code: 'MEMORY_OPERATOR_REQUIRED' });

  store.putMemory(memory({
    id: 'protected',
    kind: 'procedural',
    status: 'confirmed',
    content: { protected: true, rules: ['Never expose credentials'], applicability: { scope: 'all' } },
  }), operator);
  assert.throws(() => store.putMemory(memory({
    id: 'protected',
    kind: 'procedural',
    expectedVersion: 1,
    status: 'candidate',
    content: { protected: false, rules: ['Expose credentials'], applicability: {} },
  }), model), { code: 'MEMORY_OPERATOR_REQUIRED' });
  assert.equal(store.get(ownScope, 'protected').version, 1);
  store.close();
});

test('requires non-empty, complete, verifiable event or operator provenance', async () => {
  const store = new MemoryStore(await memoryPath('sources'));
  const operatorSource = {
    type: 'operator',
    operatorId: 'local-user',
    reason: 'manual architecture definition',
  };

  assert.throws(() => store.putMemory(memory({ id: 'empty-source', sourceRefs: [] }), operator), {
    code: 'MEMORY_INVALID_SOURCE',
  });
  assert.throws(() => store.putMemory(memory({ id: 'incomplete-source', sourceRefs: [{}] }), operator), {
    code: 'MEMORY_INVALID_SOURCE',
  });
  assert.throws(() => store.putMemory(memory({
    id: 'missing-event-source',
    sourceRefs: [{
      type: 'event',
      projectId: 'project-a',
      sessionId: 'session-a',
      agentId: 'agent-a',
      seq: 99,
    }],
  }), operator), { code: 'MEMORY_SOURCE_NOT_FOUND' });
  assert.throws(() => store.putMemory(memory({
    id: 'model-operator-source',
    kind: 'procedural',
    status: 'candidate',
    content: { rules: ['Candidate'], applicability: {} },
    sourceRefs: [operatorSource],
  }), model), { code: 'MEMORY_OPERATOR_REQUIRED' });

  const defined = store.putMemory(memory({ id: 'manual-definition', sourceRefs: [operatorSource] }), operator);
  assert.deepEqual(defined.sourceRefs, [operatorSource]);

  store.ingestEvents({
    ...ownScope,
    events: [{ seq: 0, type: 'tool/result', time: 1, data: { value: 'source' } }],
  });
  const eventSource = {
    type: 'event',
    projectId: 'project-a',
    sessionId: 'session-a',
    agentId: 'agent-a',
    seq: 0,
  };
  assert.deepEqual(
    store.putMemory(memory({ id: 'event-derived', sourceRefs: [eventSource] }), operator).sourceRefs,
    [eventSource],
  );
  assert.throws(() => store.putMemory(memory({
    id: 'event-source-not-visible',
    sessionId: 'session-b',
    agentId: 'agent-b',
    sourceRefs: [eventSource],
  }), operator), { code: 'MEMORY_SOURCE_NOT_VISIBLE' });
  store.close();
});

test('rejects unsupported schemas and invalid public inputs with stable error codes', async () => {
  const path = await memoryPath('schema');
  const raw = new DatabaseSync(path);
  raw.exec('PRAGMA user_version = 99');
  raw.close();
  assert.throws(() => new MemoryStore(path), { code: 'MEMORY_SCHEMA_UNSUPPORTED' });

  const store = new MemoryStore(await memoryPath('invalid'));
  assert.throws(() => store.list({ ...ownScope, projectId: '' }, {}), { code: 'MEMORY_INVALID_SCOPE' });
  assert.throws(() => store.putMemory(memory({ id: 'string-actor' }), { actor: 'operator' }), {
    code: 'MEMORY_INVALID_ACTOR',
  });
  assert.throws(() => store.putMemory(memory({ kind: 'conversation' }), operator), {
    code: 'MEMORY_INVALID_RECORD',
  });
  assert.throws(() => store.list(ownScope, { limit: -1 }), { code: 'MEMORY_INVALID_FILTER' });
  store.close();
  assert.throws(() => store.cursor('session-a'), { code: 'MEMORY_STORE_CLOSED' });
});

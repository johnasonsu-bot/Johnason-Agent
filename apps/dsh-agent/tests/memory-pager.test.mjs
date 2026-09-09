import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { MemoryPager } from '../src/memory-pager.mjs';
import { MemoryStore } from '../src/memory-store.mjs';

const owner = Object.freeze({ projectId: 'project', sessionId: 'session', agentId: 'owner' });
const operator = Object.freeze({ actor: { id: 'local-user', role: 'operator' } });

async function createPager(name) {
  const directory = await mkdtemp(join(tmpdir(), `dsh-pager-${name}-`));
  const store = new MemoryStore(join(directory, 'memory.sqlite'));
  return { store, pager: new MemoryPager(store) };
}

function semantic(id, summary, content, visibility = 'project') {
  return {
    id,
    kind: 'semantic',
    ...owner,
    visibility,
    content: { nodes: [{ id, type: 'fact', label: content }], edges: [] },
    summary,
    sourceRefs: [{
      type: 'operator',
      operatorId: 'local-user',
      reason: 'unit-test definition',
    }],
  };
}

test('search returns bounded summary handles without content and respects private ownership', async () => {
  const { store, pager } = await createPager('search');
  store.putMemory(semantic('alpha', 'Needle architecture', 'private detail'), operator);
  store.putMemory(semantic('beta', 'Needle database', 'shared detail'), operator);
  store.putMemory(semantic('private', 'Needle secret', 'owner-only detail', 'private'), operator);

  const handles = pager.search(owner, 'needle', { limit: 2 });
  assert.equal(handles.length, 2);
  assert.deepEqual(Object.keys(handles[0]).sort(), [
    'id', 'kind', 'sourceRefs', 'status', 'summary', 'systemTime', 'validTime', 'version', 'visibility',
  ]);
  assert.equal(handles.some(handle => Object.hasOwn(handle, 'content')), false);

  const other = { projectId: 'project', sessionId: 'other-session', agentId: 'other-agent' };
  assert.equal(pager.search(other, 'secret', { limit: 10 }).some(handle => handle.id === 'private'), false);
  store.close();
});

test('search matches across the complete scoped history before limiting candidates', async () => {
  const { store, pager } = await createPager('old-search');
  store.putMemory(semantic('old-fact', 'Historic match', 'RARE-TAIL-FACT'), operator);
  for (let i = 0; i < 1001; i++) store.putMemory(semantic(`noise-${i}`, 'Recent noise', 'irrelevant'), operator);
  const hits = pager.search(owner, 'rare-tail-fact', { limit: 1 });
  assert.deepEqual(hits.map(hit => hit.id), ['old-fact']);
  assert.equal(Object.hasOwn(hits[0], 'content'), false);
  store.close();
});

test('bounded continuation reaches the tail and reopens the exact historical version', async () => {
  const { store, pager } = await createPager('tail-pages');
  const original = store.putMemory(semantic('long-tail', 'Tail record', 'x'.repeat(110000) + 'TAIL-ONLY-MARKER'), operator);
  const full = JSON.stringify(original.content);
  const first = pager.pageIn(owner, original.id, { maxChars: 100000 });
  assert.equal(first.totalChars, full.length);
  assert.equal(first.nextOffset, 100000);
  assert.equal(first.hasMore, true);
  const args = { version: first.version, offset: first.nextOffset, maxChars: 20000 };
  const tail = pager.pageIn(owner, original.id, args);
  assert.equal(tail.contentText, full.slice(100000));
  assert.equal(tail.hasMore, false); assert.equal(tail.nextOffset, null);
  assert.match(pager.select(owner, { budgetTokens: 5000 }).contextText, /TAIL-ONLY-MARKER/);
  store.putMemory({ ...semantic('long-tail', 'New version', 'replacement'), expectedVersion: 1 }, operator);
  const path = store.db.location(); store.close();
  const reopened = new MemoryStore(path);
  assert.equal(new MemoryPager(reopened).pageIn(owner, original.id, args).contentText, tail.contentText);
  assert.throws(() => new MemoryPager(reopened).pageIn(owner, original.id, { offset: -1 }), { code: 'MEMORY_INVALID_OFFSET' });
  assert.throws(() => new MemoryPager(reopened).pageIn(owner, original.id, { version: 1, offset: full.length + 1 }), { code: 'MEMORY_INVALID_OFFSET' });
  reopened.close();
});

test('page in exposes bounded content, page out changes selected context, and neither deletes source records', async () => {
  const { store, pager } = await createPager('page-in-out');
  store.putMemory(semantic('long', 'Needle long record', 'x'.repeat(200)), operator);

  const before = pager.select(owner, { query: '', budgetTokens: 200, anchors: ['Task goal'] });
  assert.equal(before.contextText.includes('xxxxxxxx'), false);

  const page = pager.pageIn(owner, 'long', { maxChars: 80 });
  assert.equal(page.contentText.length, 80);
  assert.equal(page.truncated, true);
  assert.deepEqual(page.sourceRefs, [{
    type: 'operator',
    operatorId: 'local-user',
    reason: 'unit-test definition',
  }]);

  const during = pager.select(owner, { query: '', budgetTokens: 200, anchors: ['Task goal'] });
  assert.equal(during.contextText.includes('xxxxxxxx'), true);
  assert.equal(during.pages[0].id, 'long');
  assert.equal(pager.pageOut(owner, 'long'), true);
  assert.equal(pager.pageOut(owner, 'long'), false);

  const after = pager.select(owner, { query: '', budgetTokens: 200, anchors: ['Task goal'] });
  assert.equal(after.contextText.includes('xxxxxxxx'), false);
  assert.equal(store.get(owner, 'long').id, 'long');
  store.close();
});

test('selection keeps mandatory anchors intact and never exceeds its estimated token budget', async () => {
  const { store, pager } = await createPager('budget');
  store.putMemory(semantic('one', 'Needle first useful memory', 'one content'), operator);
  store.putMemory(semantic('two', 'Needle second useful memory', 'two content'), operator);

  const result = pager.select(owner, {
    query: 'needle',
    budgetTokens: 24,
    anchors: [{ label: 'goal', text: 'Keep this anchor unchanged' }],
  });
  assert.equal(result.contextText.includes('Keep this anchor unchanged'), true);
  assert.equal(result.estimatedTokens, Math.ceil(result.contextText.length / 4));
  assert.ok(result.estimatedTokens <= 24);
  assert.equal(result.pages.every(page => Object.hasOwn(page, 'content') === false), true);

  assert.throws(() => pager.select(owner, {
    query: 'needle',
    budgetTokens: 2,
    anchors: ['This mandatory invariant cannot fit'],
  }), { code: 'MEMORY_BUDGET_EXCEEDED' });
  store.close();
});

test('selection treats a tool call and its sourced result as one budget unit', async () => {
  const { store, pager } = await createPager('tool-pair');
  store.ingestEvents({
    ...owner,
    events: [
      { seq: 0, type: 'tool/call', time: 1, data: { name: 'read_file' } },
      {
        seq: 1,
        type: 'tool/result',
        time: 2,
        data: { value: 'paired-needle' },
        sourceEventSeqs: [0],
      },
    ],
  });

  const roomy = pager.select(owner, { query: 'paired-needle', budgetTokens: 200, anchors: [] });
  assert.deepEqual(roomy.pages.map(page => page.id), [
    'event:session:0',
    'event:session:1',
  ]);

  const tight = pager.select(owner, { query: 'paired-needle', budgetTokens: 35, anchors: [] });
  assert.notEqual(tight.pages.length, 1);
  store.close();
});

test('selection resolves a linked call exactly when it falls outside the latest episodic window', async () => {
  const { store, pager } = await createPager('tool-pair-window');
  const events = [
    { seq: 0, type: 'tool/call', time: 1, data: { name: 'read_file' } },
    ...Array.from({ length: 1_000 }, (_, index) => ({
      seq: index + 1,
      type: 'assistant/chunk',
      time: index + 2,
      data: { text: `filler-${index + 1}` },
    })),
    {
      seq: 1_001,
      type: 'tool/result',
      time: 1_002,
      data: { value: 'far-paired-needle' },
      sourceEventSeqs: [0],
    },
  ];
  store.ingestEvents({ ...owner, events });

  const result = pager.select(owner, { query: 'far-paired-needle', budgetTokens: 200, anchors: [] });
  assert.deepEqual(result.pages.map(page => page.id), [
    'event:session:0',
    'event:session:1001',
  ]);
  store.close();
});

test('selection omits an entire linked group when a source has no visible projection', async () => {
  const { store, pager } = await createPager('tool-pair-missing');
  store.ingestEvents({
    ...owner,
    events: [
      { seq: 0, type: 'vault/credentials', time: 1, data: { value: 'filtered' } },
      {
        seq: 1,
        type: 'tool/result',
        time: 2,
        data: { value: 'missing-paired-needle' },
        sourceEventSeqs: [0],
      },
    ],
  });

  const result = pager.select(owner, { query: 'missing-paired-needle', budgetTokens: 200, anchors: [] });
  assert.deepEqual(result.pages, []);
  assert.equal(result.contextText, '');
  store.close();
});

test('selection resolves reverse and transitive dependencies outside the episodic window', async () => {
  const { store, pager } = await createPager('tool-pair-reverse-window');
  const events = [
    { seq: 0, type: 'tool/call', time: 1, data: { name: 'read_file' } },
    {
      seq: 1,
      type: 'tool/result',
      time: 2,
      data: { value: 'result' },
      sourceEventSeqs: [0],
    },
    ...Array.from({ length: 1_000 }, (_, index) => ({
      seq: index + 2,
      type: 'assistant/chunk',
      time: index + 3,
      data: { text: `later-${index + 2}` },
    })),
  ];
  store.ingestEvents({ ...owner, events });
  pager.pageIn(owner, 'event:session:0', { maxChars: 100 });

  const result = pager.select(owner, { query: '', budgetTokens: 200, anchors: [] });
  assert.deepEqual(result.pages.map(page => page.id), [
    'event:session:0',
    'event:session:1',
  ]);
  store.close();
});

test('validates empty, negative, and oversized paging inputs', async () => {
  const { store, pager } = await createPager('invalid');
  store.putMemory(semantic('one', 'Needle', 'content'), operator);

  assert.throws(() => pager.search(owner, '', { limit: 2 }), { code: 'MEMORY_INVALID_QUERY' });
  assert.throws(() => pager.search(owner, 'needle', { limit: 0 }), { code: 'MEMORY_INVALID_LIMIT' });
  assert.throws(() => pager.pageIn(owner, 'one', { maxChars: -1 }), { code: 'MEMORY_INVALID_LIMIT' });
  assert.throws(() => pager.pageIn(owner, 'one', { maxChars: 100_001 }), { code: 'MEMORY_INVALID_LIMIT' });
  assert.throws(() => pager.select(owner, { query: '', budgetTokens: 0, anchors: [] }), {
    code: 'MEMORY_INVALID_BUDGET',
  });
  store.close();
});

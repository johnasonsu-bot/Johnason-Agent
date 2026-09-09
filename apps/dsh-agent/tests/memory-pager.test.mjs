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
    sourceRefs: [{ sessionId: 'session', seq: id.length }],
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

test('page in exposes bounded content, page out changes selected context, and neither deletes source records', async () => {
  const { store, pager } = await createPager('page-in-out');
  store.putMemory(semantic('long', 'Needle long record', 'x'.repeat(200)), operator);

  const before = pager.select(owner, { query: '', budgetTokens: 200, anchors: ['Task goal'] });
  assert.equal(before.contextText.includes('xxxxxxxx'), false);

  const page = pager.pageIn(owner, 'long', { maxChars: 80 });
  assert.equal(page.contentText.length, 80);
  assert.equal(page.truncated, true);
  assert.deepEqual(page.sourceRefs, [{ sessionId: 'session', seq: 4 }]);

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

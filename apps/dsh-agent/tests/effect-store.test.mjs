import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { MemoryStore } from '../src/memory-store.mjs';
import { EffectStore } from '../src/effect-store.mjs';

const scope = { projectId: 'p', sessionId: 's', agentId: 'a' };
const owner = { id: 'worker-a', scope };
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'dsh-effects-'));
  t.diagnostic(`Retained fixture: ${root}`);
  const path = join(root, 'memory.sqlite');
  const memory = new MemoryStore(path);
  t.after(() => memory.close());
  const effects = new EffectStore(memory);
  const input = { ...scope, visibility: 'private', callId: 'call-1', stepId: 'step-1',
    toolName: 'shell', input: { b: 2, a: 1 }, workspaceRoot: root,
    sandboxPolicy: { mode: 'workspace-write', network: 'host' }, validTime: 10 };
  return { root, path, memory, effects, input };
}

test('durable reservation and owner CAS admit one real child side effect only', t => {
  const { effects, input, root } = fixture(t);
  const prepared = effects.prepare(input);
  assert.equal(prepared.state, 'PREPARED');
  assert.equal(prepared.protocol, 'reservation-execution-confirmation');
  assert.equal(effects.prepare({ ...input, input: { a: 1, b: 2 } }).id, prepared.id);
  assert.throws(() => effects.prepare({ ...input, input: { a: 2 } }), { code: 'EFFECT_ID_CONFLICT' });
  effects.begin(prepared.id, owner, { validTime: 15 });
  assert.throws(() => effects.begin(prepared.id, { id: 'worker-b', scope }), { code: 'EFFECT_STATE_CONFLICT' });
  const counter = join(root, 'counter');
  const child = spawnSync(process.execPath, ['-e', 'require("node:fs").appendFileSync(process.argv[1],"1")', counter]);
  assert.equal(child.status, 0);
  assert.throws(() => effects.finish(prepared.id, { id: 'worker-b', scope }, { state: 'COMMITTED' }), { code: 'EFFECT_OWNER_CONFLICT' });
  const done = effects.finish(prepared.id, owner, { state: 'COMMITTED', result: { exitCode: 0 }, validTime: 20 });
  assert.equal(effects.prepare(input).state, 'COMMITTED');
  assert.equal(done.result.exitCode, 0);
  assert.throws(() => effects.begin(prepared.id, owner), { code: 'EFFECT_STATE_CONFLICT' });
  assert.equal(readFileSync(counter, 'utf8'), '1');
});

test('restart recovery distinguishes prepared intent and unknown dispatch without retry', t => {
  const { effects, input, memory, path } = fixture(t);
  const dispatched = effects.prepare(input);
  effects.begin(dispatched.id, owner);
  const pending = effects.prepare({ ...input, callId: 'pending' });
  memory.close();
  const reopened = new MemoryStore(path);
  t.after(() => reopened.close());
  const restored = new EffectStore(reopened);
  assert.equal(reopened.db.prepare('PRAGMA user_version').get().user_version, 1);
  const states = restored.recover(scope);
  assert.equal(states.find(x => x.id === dispatched.id).state, 'UNKNOWN');
  assert.equal(states.find(x => x.id === pending.id).state, 'PREPARED');
  assert.throws(() => restored.begin(dispatched.id, owner), { code: 'EFFECT_STATE_CONFLICT' });
  assert.throws(() => restored.finish(dispatched.id, owner, { state: 'COMMITTED' }), { code: 'EFFECT_STATE_CONFLICT' });
});

test('scope is enforced for writes, reads, identity and causal edges', t => {
  const { effects, input } = fixture(t);
  const first = effects.prepare(input);
  const second = effects.prepare({ ...input, callId: 'call-2', parentId: first.id, causationId: first.id });
  assert.notEqual(effects.prepare({ ...input, sessionId: 'other' }).id, first.id);
  const deniedScope = { ...scope, agentId: 'other' };
  assert.deepEqual(effects.list(deniedScope), []);
  assert.throws(() => effects.begin(first.id, { id: 'bad', scope: deniedScope }), { code: 'EFFECT_NOT_FOUND' });
  assert.throws(() => effects.prepare({ ...input, callId: 'bad-edge', parentId: 'missing' }), { code: 'EFFECT_CAUSE_NOT_FOUND' });
  const graph = effects.graph(scope);
  assert.ok(graph.edges.some(x => x.from === first.id && x.to === second.id && x.relation === 'parent'));
});

test('append-only bitemporal correction retains past knowledge and requires readback proof', t => {
  const { effects, input, memory } = fixture(t);
  const prepared = effects.prepare(input);
  const started = effects.begin(prepared.id, owner, { validTime: 20 });
  const unknown = effects.finish(prepared.id, owner, { state: 'UNKNOWN', validTime: 30 });
  assert.equal(effects.list(scope, { systemAt: started.systemTime })[0].state, 'EXECUTING');
  assert.equal(effects.list(scope, { validAt: 15 })[0].state, 'PREPARED');
  assert.throws(() => effects.reconcile(prepared.id, { scope, state: 'COMMITTED', verified: true }), { code: 'EFFECT_PROOF_REQUIRED' });
  assert.equal(effects.reconcile(prepared.id, { scope, kind: 'manual', reason: 'looks done' }).state, 'UNKNOWN');
  const corrected = effects.reconcile(prepared.id, { scope, kind: 'adapter-readback', adapterId: 'counter-readback',
    readback: () => ({ state: 'COMMITTED', result: { count: 1 }, evidence: { receipt: 'file-count-1' } }), validTime: 30 });
  assert.equal(corrected.state, 'COMMITTED');
  assert.equal(effects.list(scope, { systemAt: unknown.systemTime })[0].state, 'UNKNOWN');
  assert.throws(() => memory.db.exec('DELETE FROM effect_events'), /append-only/);
  assert.throws(() => memory.db.exec("UPDATE effect_events SET state='ABORTED'"), /append-only/);
});

test('rejects ambiguous input and unsupported participant protocol before admission', t => {
  const { effects, input } = fixture(t);
  assert.throws(() => effects.prepare({ ...input, input: { value: undefined } }), { code: 'EFFECT_INVALID_INPUT' });
  assert.throws(() => effects.prepare({ ...input, protocol: 'participant-2pc' }), { code: 'EFFECT_PROTOCOL_UNSUPPORTED' });
  assert.deepEqual(effects.list(scope), []);
});

test('independent SQLite connections cannot dispatch an already acquired effect', t => {
  const { effects, input, path } = fixture(t);
  const memoryB = new MemoryStore(path);
  t.after(() => memoryB.close());
  const other = new EffectStore(memoryB);
  const first = effects.prepare(input);
  assert.equal(other.prepare(input).id, first.id);
  effects.begin(first.id, owner);
  assert.throws(() => other.begin(first.id, { id: 'other-process', scope }), { code: 'EFFECT_STATE_CONFLICT' });
});

test('retroactive completion supersedes unknown while preserving valid and system history', t => {
  const { effects, input, memory, path } = fixture(t);
  const prepared = effects.prepare(input);
  effects.begin(prepared.id, owner, { validTime: 20 });
  const unknown = effects.finish(prepared.id, owner, { state: 'UNKNOWN', validTime: 30 });
  const corrected = effects.reconcile(prepared.id, { scope, kind: 'adapter-readback', adapterId: 'receipt-reader', validTime: 25,
    readback: () => ({ state: 'COMMITTED', evidence: { receipt: 'completed-at-25' } }) });
  assert.equal(corrected.validTime, 25);
  assert.equal(effects.prepare(input).state, 'COMMITTED');
  assert.equal(effects.list(scope)[0].state, 'COMMITTED');
  assert.equal(effects.graph(scope).nodes[0].state, 'COMMITTED');
  assert.equal(effects.list(scope, { validAt: 24 })[0].state, 'EXECUTING');
  assert.equal(effects.list(scope, { validAt: 25 })[0].state, 'COMMITTED');
  assert.equal(effects.list(scope, { validAt: 31, systemAt: unknown.systemTime })[0].state, 'UNKNOWN');
  assert.equal(effects.list(scope, { validAt: 26, systemAt: unknown.systemTime })[0].state, 'EXECUTING');
  memory.close();
  const reopened = new MemoryStore(path);
  t.after(() => reopened.close());
  assert.equal(new EffectStore(reopened).list(scope)[0].state, 'COMMITTED');
});

test('ordinary transitions cannot backdate before prior state, correction cannot predate dispatch', t => {
  const { effects, input } = fixture(t);
  const prepared = effects.prepare(input);
  assert.throws(() => effects.begin(prepared.id, owner, { validTime: 9 }), { code: 'EFFECT_INVALID_TIME_ORDER' });
  effects.begin(prepared.id, owner, { validTime: 20 });
  assert.throws(() => effects.finish(prepared.id, owner, { state: 'COMMITTED', validTime: 19 }), { code: 'EFFECT_INVALID_TIME_ORDER' });
  effects.finish(prepared.id, owner, { state: 'UNKNOWN', validTime: 30 });
  assert.throws(() => effects.reconcile(prepared.id, { scope, kind: 'adapter-readback', adapterId: 'receipt-reader', validTime: 19,
    readback: () => ({ state: 'COMMITTED', evidence: { receipt: 'invalid-before-dispatch' } }) }), { code: 'EFFECT_INVALID_TIME_ORDER' });
  assert.equal(effects.list(scope)[0].state, 'UNKNOWN');
});

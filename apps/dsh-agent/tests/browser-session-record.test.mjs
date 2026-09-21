import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import test from 'node:test';
import { browserSessionRecord } from '../src/browser-session-record.mjs';

const grant = () => ({ kind: 'grant', payload: { version: 1, secret: randomBytes(32).toString('base64url') } });

test('browser startup auth is shared within one app root and isolated across restarts', async () => {
  const root = {};
  const store = browserSessionRecord(root);
  const first = await store.modify(() => grant());
  assert.equal(first.changed, true);
  assert.deepEqual(await browserSessionRecord(root).read(), first.record);
  assert.equal(await browserSessionRecord({}).read(), undefined);
});

test('concurrent browser activation keeps one signing record and does not expose mutable storage', async () => {
  const store = browserSessionRecord({});
  const initialize = () => store.modify(async current => current === undefined ? grant() : undefined);
  const [first, second] = await Promise.all([initialize(), initialize()]);
  assert.deepEqual(first.record, second.record);
  assert.equal(second.changed, false);
  const original = await store.read();
  first.record.payload.secret = 'tampered';
  await store.modify(current => { current.payload.secret = 'mutated-only-copy'; });
  assert.deepEqual(await store.read(), original);
});

test('browser startup storage rejects model secrets and malformed signing records without poisoning later calls', async () => {
  const store = browserSessionRecord({});
  for (const record of [
    { kind: 'api-key', key: 'synthetic-test-value' },
    { kind: 'grant', payload: { version: 2, secret: randomBytes(32).toString('base64url') } },
    { kind: 'grant', payload: { version: 1, secret: 'invalid' } },
    { ...grant(), extra: true },
  ]) await assert.rejects(store.modify(() => record), /browser signing record/);
  assert.equal(await store.read(), undefined);
  await store.modify(() => grant());
  assert.equal(await store.clear(), true);
  assert.equal(await store.clear(), false);
});

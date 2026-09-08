import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { VaultStore } from '../src/vault-store.mjs';
import { createVaultHandler } from '../src/vault-ui.mjs';

test('local same-origin UI initializes, unlocks, locks and rejects hostile requests', async t => {
  const vault = new VaultStore(join(await mkdtemp(join(tmpdir(), 'dsh-ui-')), 'vault.enc'));
  const server = createServer(createVaultHandler(vault));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { vault.lock(); server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const url = `http://127.0.0.1:${server.address().port}`;
  const post = (action, body, origin = url) => fetch(`${url}/vault/${action}`, { method: 'POST', headers: { origin, 'content-type': 'application/json' }, body: JSON.stringify(body) });
  assert.deepEqual(await (await fetch(`${url}/vault/status`)).json(), { initialized: false, locked: true });
  assert.equal((await post('initialize', { password: 'unit-pass', confirmation: 'different' })).status, 400);
  assert.equal((await post('initialize', { password: 'unit-pass', confirmation: 'unit-pass' })).status, 200);
  assert.equal((await post('lock', {})).status, 200);
  const failure = await post('unlock', { password: 'wrong-secret' });
  assert.equal(failure.status, 400);
  assert.equal((await failure.text()).includes('wrong-secret'), false);
  assert.deepEqual(await vault.status(), { initialized: true, locked: true });
  assert.equal((await post('unlock', { password: 'unit-pass' }, 'https://evil.test')).status, 403);
  assert.equal((await post('unlock', { password: 'x'.repeat(20000) })).status, 413);
  assert.equal((await post('unlock', { password: 'unit-pass' })).status, 200);
  assert.equal((await fetch(`${url}/vault`)).status, 200);
});

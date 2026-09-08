import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import test from 'node:test';

import { VaultStore } from '../src/vault-store.mjs';

async function vaultPath(name) {
  const directory = await mkdtemp(join(tmpdir(), `dsh-vault-${name}-`));
  return join(directory, 'vault.json');
}

async function runChild(modulePath, file, key, value, delay = 0) {
  const source = `
    import { VaultStore } from ${JSON.stringify(modulePath)};
    const vault = new VaultStore(process.argv[1]);
    await vault.unlock('unit-test-passphrase');
    await vault.update(async data => {
      await new Promise(resolve => setTimeout(resolve, Number(process.argv[4])));
      data.records[process.argv[2]] = { secret: process.argv[3] };
    });
  `;
  const child = spawn(process.execPath, ['--input-type=module', '-e', source, file, key, value, String(delay)], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr += chunk; });
  const code = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', resolve);
  });
  assert.equal(code, 0, stderr);
}

test('encrypts data at rest and clears access when locked', async () => {
  const file = await vaultPath('encrypted');
  const vault = new VaultStore(file);

  assert.deepEqual(await vault.status(), { initialized: false, locked: true });
  await vault.initialize('unit-test-passphrase');
  await vault.update(data => { data.refs.TEST_KEY = 'unit-test-value'; });

  assert.equal((await readFile(file, 'utf8')).includes('unit-test-value'), false);
  assert.deepEqual(await vault.status(), { initialized: true, locked: false });
  vault.lock();
  assert.deepEqual(await vault.status(), { initialized: true, locked: true });
  await assert.rejects(() => vault.read(), { code: 'VAULT_LOCKED' });

  await vault.unlock('unit-test-passphrase');
  assert.equal((await vault.read()).refs.TEST_KEY, 'unit-test-value');
});

test('does not overwrite an initialized vault or modify it after a wrong password', async () => {
  const file = await vaultPath('preserve');
  const first = new VaultStore(file);
  await first.initialize('unit-test-passphrase');
  await first.update(data => { data.records.account = { token: 'synthetic-token' }; });
  first.lock();
  const before = await readFile(file);

  await assert.rejects(() => new VaultStore(file).initialize('replacement'), { code: 'VAULT_EXISTS' });
  await assert.rejects(() => new VaultStore(file).unlock('wrong-password'), { code: 'VAULT_UNLOCK_FAILED' });
  assert.deepEqual(await readFile(file), before);
});

test('reports malformed envelopes without resetting the file', async () => {
  const file = await vaultPath('malformed');
  await writeFile(file, '{"version":1,"ciphertext":"not-an-envelope"}', { mode: 0o600 });
  const before = await readFile(file);

  await assert.rejects(() => new VaultStore(file).unlock('unit-test-passphrase'), { code: 'VAULT_FORMAT_ERROR' });
  assert.deepEqual(await readFile(file), before);
});

test('reports non-string encoded envelope fields as a vault format error', async () => {
  const file = await vaultPath('field-type');
  await writeFile(file, JSON.stringify({
    version: 1,
    kdf: { name: 'scrypt', N: 16384, r: 8, p: 1 },
    salt: 'AAAAAAAAAAAAAAAAAAAAAA==',
    nonce: 'AAAAAAAAAAAAAAAA',
    tag: 'AAAAAAAAAAAAAAAAAAAAAA==',
    ciphertext: 42,
  }), { mode: 0o600 });

  await assert.rejects(() => new VaultStore(file).unlock('unit-test-passphrase'), {
    code: 'VAULT_FORMAT_ERROR',
  });
});

test('lock invalidates unlock and initialize operations already awaiting filesystem or KDF work', async () => {
  const unlockFile = await vaultPath('unlock-generation');
  const prepared = new VaultStore(unlockFile);
  await prepared.initialize('unit-test-passphrase');
  prepared.lock();

  const unlocking = prepared.unlock('unit-test-passphrase');
  prepared.lock();
  await assert.rejects(() => unlocking, { code: 'VAULT_LOCKED' });
  assert.deepEqual(await prepared.status(), { initialized: true, locked: true });

  const initializeFile = await vaultPath('initialize-generation');
  const initializingStore = new VaultStore(initializeFile);
  const initializing = initializingStore.initialize('unit-test-passphrase');
  initializingStore.lock();
  await assert.rejects(() => initializing, { code: 'VAULT_LOCKED' });
  assert.deepEqual(await initializingStore.status(), { initialized: false, locked: true });
});

test('serializes two instances and preserves asynchronous read-modify-write updates', async () => {
  const file = await vaultPath('instances');
  const first = new VaultStore(file);
  const second = new VaultStore(file);
  await first.initialize('unit-test-passphrase');
  await second.unlock('unit-test-passphrase');

  const slow = first.update(async data => {
    await new Promise(resolve => setTimeout(resolve, 80));
    data.records.first = { value: 'one' };
  });
  const fast = second.update(data => { data.records.second = { value: 'two' }; });
  await Promise.all([slow, fast]);

  const result = await first.read();
  assert.deepEqual(result.records, {
    first: { value: 'one' },
    second: { value: 'two' },
  });
});

test('serializes independent processes without losing either record', async () => {
  const file = await vaultPath('processes');
  const vault = new VaultStore(file);
  await vault.initialize('unit-test-passphrase');
  vault.lock();
  const modulePath = new URL('../src/vault-store.mjs', import.meta.url).href;

  await Promise.all([
    runChild(modulePath, file, 'alpha', 'synthetic-alpha', 100),
    runChild(modulePath, file, 'beta', 'synthetic-beta', 0),
  ]);

  await vault.unlock('unit-test-passphrase');
  assert.deepEqual((await vault.read()).records, {
    alpha: { secret: 'synthetic-alpha' },
    beta: { secret: 'synthetic-beta' },
  });
});

test('locking during an update prevents commit and leaves the vault usable', async () => {
  const file = await vaultPath('lock-race');
  const vault = new VaultStore(file);
  await vault.initialize('unit-test-passphrase');
  let resume;
  const paused = new Promise(resolve => { resume = resolve; });
  let entered;
  const started = new Promise(resolve => { entered = resolve; });

  const update = vault.update(async data => {
    entered();
    await paused;
    data.refs.RACE = 'must-not-commit';
  });
  await started;
  vault.lock();
  resume();
  await assert.rejects(() => update, { code: 'VAULT_LOCKED' });

  await vault.unlock('unit-test-passphrase');
  assert.equal((await vault.read()).refs.RACE, undefined);
  assert.equal((await stat(file)).isFile(), true);
});

test('locking after update prepares its temporary file prevents the rename commit', async () => {
  const file = await vaultPath('prepare-race');
  const vault = new VaultStore(file);
  await vault.initialize('unit-test-passphrase');
  const directory = new URL('.', `file://${file}`).pathname;
  const base = file.slice(file.lastIndexOf('/') + 1);

  const update = vault.update(data => {
    data.records.large = { padding: 'x'.repeat(48 * 1024 * 1024) };
  });

  const deadline = Date.now() + 5_000;
  while (!(await readdir(directory)).some(name => name.startsWith(`${base}.tmp-`))) {
    if (Date.now() >= deadline) assert.fail('update did not expose its prepared temporary file');
    await new Promise(resolve => setImmediate(resolve));
  }
  vault.lock();
  await assert.rejects(() => update, { code: 'VAULT_LOCKED' });

  await vault.unlock('unit-test-passphrase');
  assert.equal((await vault.read()).records.large, undefined);
});

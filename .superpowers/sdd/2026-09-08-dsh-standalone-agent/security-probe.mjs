import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { mkdtemp, readFile, writeFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { VaultStore } from '../../../apps/dsh-agent/src/vault-store.mjs';
import { createVaultHandler } from '../../../apps/dsh-agent/src/vault-ui.mjs';

// Isolated negative-path probe; generated canaries are not real credentials.
const root = await mkdtemp(join(tmpdir(), 'dsh-security-probe-'));
const file = join(root, 'vault.enc');
const vault = new VaultStore(file);
const password = randomUUID();
const canary = randomUUID();
const server = createServer(createVaultHandler(vault));
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const url = `http://127.0.0.1:${server.address().port}`;
let checks = 0;
const post = (body, headers = {}, action = 'unlock') => new Promise((resolve, reject) => {
  const req = request(`${url}/vault/${action}`, {
    method: 'POST', headers: { origin: url, 'content-type': 'application/json', ...headers },
  }, response => {
    const chunks = [];
    response.on('data', chunk => chunks.push(chunk));
    response.on('end', () => resolve({ status: response.statusCode, text: async () => Buffer.concat(chunks).toString('utf8') }));
  });
  req.on('error', reject);
  req.end(typeof body === 'string' ? body : JSON.stringify(body));
});
try {
  assert.equal((await post({ password, confirmation: password }, {}, 'initialize')).status, 200); checks++;
  await vault.update(data => { data.records.canary = { value: canary }; });
  const original = await readFile(file);
  assert.ok(!original.includes(password) && !original.includes(canary)); checks++;
  assert.equal((await stat(file)).mode & 0o777, 0o600); checks++;
  vault.lock();
  for (const headers of [
    { origin: 'https://outside.invalid' }, { host: 'outside.invalid' },
    { 'sec-fetch-site': 'cross-site' }, { 'content-type': 'text/plain' },
  ]) { assert.equal((await post({ password }, headers)).status, 403); checks++; }
  assert.equal((await post({ password: 'x'.repeat(20000) })).status, 413); checks++;
  assert.equal((await post('{ malformed')).status, 400); checks++;
  const hostile = '<script>PROBE_ONLY</script>';
  const response = await post({ password: hostile });
  assert.equal(response.status, 400);
  assert.ok(!(await response.text()).includes(hostile)); checks++;
  assert.deepEqual(await vault.status(), { initialized: true, locked: true });
  assert.deepEqual(await readFile(file), original); checks++;
  assert.equal((await post({ password })).status, 200); checks++;
  assert.equal((await vault.read()).records.canary.value, canary); checks++;
  vault.lock();
  const envelope = JSON.parse(original);
  const corrupted = Buffer.from(envelope.tag, 'base64'); corrupted[0] ^= 1;
  envelope.tag = corrupted.toString('base64');
  await writeFile(file, JSON.stringify(envelope));
  const tampered = await readFile(file);
  assert.equal((await post({ password })).status, 400);
  assert.deepEqual(await vault.status(), { initialized: true, locked: true });
  assert.deepEqual(await readFile(file), tampered); checks++;
  console.log(JSON.stringify({ result: 'pass', checks, root, scope: 'isolated Vault HTTP and ciphertext negative paths' }));
} finally {
  vault.lock(); server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}

import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
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
  assert.deepEqual(await failure.json(), {
    code: 'VAULT_UNLOCK_FAILED',
    error: '保险箱认证失败：密码可能不正确，或文件完整性可能受损。不会自动删除或重置保险箱。',
  });
  assert.deepEqual(await vault.status(), { initialized: true, locked: true });
  assert.equal((await post('unlock', { password: 'unit-pass' }, 'https://evil.test')).status, 403);
  assert.equal((await post('unlock', { password: 'x'.repeat(20000) })).status, 413);
  assert.equal((await post('unlock', { password: 'unit-pass' })).status, 200);
  assert.equal((await fetch(`${url}/vault`)).status, 200);
});

test('known vault failures have distinct fixed feedback and unknown errors never reflect secrets', async t => {
  let failure;
  const server = createServer(createVaultHandler({ unlock: async () => { throw failure; } }));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const url = `http://127.0.0.1:${server.address().port}`;
  const cases = [
    ['VAULT_UNLOCK_FAILED', '保险箱认证失败：密码可能不正确，或文件完整性可能受损。不会自动删除或重置保险箱。'],
    ['VAULT_DECRYPT_FAILED', '保险箱数据认证失败，文件可能已变化或受损。请锁定后重新解锁；不会自动删除或重置保险箱。'],
    ['VAULT_FORMAT_ERROR', '保险箱格式无效或不受支持。请检查文件或使用可靠备份恢复；修改密码无法修复格式问题。不会自动删除或重置保险箱。'],
    ['VAULT_BUSY', '保险箱正在被其他操作使用。请等待该操作完成后重试。'],
    ['VAULT_LOCKED', '保险箱已锁定，请先解锁再继续。'],
    ['VAULT_EXISTS', '保险箱已经初始化，请解锁已有保险箱；本次操作未覆盖它。'],
    ['VAULT_NOT_INITIALIZED', '保险箱尚未初始化，请先创建保险箱再解锁。'],
    ['VAULT_LOCK_OWNERSHIP_LOST', '保险箱操作锁的归属已变化。请停止并发操作后重试，不要删除锁或保险箱文件。'],
    ['unknown-code-secret', '保险箱操作失败，请重试或检查本地保险箱状态。不会自动删除或重置保险箱。'],
    ['constructor', '保险箱操作失败，请重试或检查本地保险箱状态。不会自动删除或重置保险箱。'],
  ];
  for (const [code, message] of cases) {
    failure = Object.assign(new Error('password-secret /private/path-secret', { cause: new Error('cause-secret') }), { code });
    const response = await fetch(`${url}/vault/unlock`, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' }, body: JSON.stringify({ password: 'input-secret' }) });
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { code: code.startsWith('VAULT_') ? code : 'VAULT_OPERATION_FAILED', error: message });
  }
});

test('real damaged vault format is reported separately from a wrong password', async t => {
  const path = join(await mkdtemp(join(tmpdir(), 'dsh-ui-format-')), 'vault.enc');
  await writeFile(path, '{invalid-json-secret');
  const vault = new VaultStore(path);
  const server = createServer(createVaultHandler(vault));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { vault.lock(); server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const url = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(`${url}/vault/unlock`, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' }, body: JSON.stringify({ password: 'input-secret' }) });
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), {
    code: 'VAULT_FORMAT_ERROR',
    error: '保险箱格式无效或不受支持。请检查文件或使用可靠备份恢复；修改密码无法修复格式问题。不会自动删除或重置保险箱。',
  });
});

test('split UTF-8 HTTP password bytes unlock the vault with the original Chinese password', async t => {
  const vault = new VaultStore(join(await mkdtemp(join(tmpdir(), 'dsh-ui-utf8-')), 'vault.enc'));
  await vault.initialize('中文口令');
  vault.lock();
  const firstChunk = Promise.withResolvers();
  const handler = createVaultHandler(vault);
  const server = createServer((req, res) => {
    req.once('data', () => firstChunk.resolve());
    return handler(req, res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { vault.lock(); server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const url = `http://127.0.0.1:${server.address().port}`;
  const payload = Buffer.from(JSON.stringify({ password: '中文口令' }));
  const split = payload.indexOf(Buffer.from('中')) + 1;
  const result = Promise.withResolvers();
  const req = request(`${url}/vault/unlock`, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' } }, response => {
    let body = '';
    response.on('data', chunk => { body += chunk; });
    response.on('end', () => result.resolve({ status: response.statusCode, body }));
  });
  req.on('error', result.reject);
  req.write(payload.subarray(0, split));
  await firstChunk.promise;
  req.end(payload.subarray(split));
  assert.equal((await result.promise).status, 200);
  assert.deepEqual(await vault.status(), { initialized: true, locked: false });
});

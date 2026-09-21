import assert from 'node:assert/strict';
import { registerHooks, stripTypeScriptTypes } from 'node:module';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const nativeFixture = new URL('./fixtures/credentials-source-native.mjs', import.meta.url).href;
const browserAuthSource = new URL('../../../third_party/deepseek-harness/packages/client/connection/src/browser-auth.ts', import.meta.url);
const names = new Set(['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-credentials-local', '@deepseek-ai/dsh-credentials', '@deepseek-ai/cordis']);
const hook = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith('/platform/credential-api.mjs')) return { url: nativeFixture, shortCircuit: true };
    return names.has(specifier) ? { url: nativeFixture, shortCircuit: true } : nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (url === browserAuthSource.href) return {
      format: 'module', source: stripTypeScriptTypes(readFileSync(browserAuthSource, 'utf8'), { mode: 'transform', sourceUrl: url }), shortCircuit: true,
    };
    return nextLoad(url, context);
  },
});
let Provider, BrowserAuth;
try {
  ({ default: Provider } = await import('../src/credentials-plugin.mjs'));
  ({ BrowserAuth } = await import(browserAuthSource.href));
} finally { hook.deregister(); }

function lockedProvider(root = {}) {
  const ctx = { root, updates: [] };
  const provider = new Provider(ctx, { path: '/unused-source-fixture/vault.enc', mode: 'web' });
  let accesses = 0;
  const locked = () => { accesses++; throw Object.assign(new Error('Vault is locked'), { code: 'VAULT_LOCKED' }); };
  provider.vault = { read: locked, update: locked, status: () => ({ initialized: true, locked: true }) };
  return { provider, root, ctx, accesses: () => accesses };
}

function authorize(auth) {
  const launch = new URL(auth.authenticatedUrl('http://127.0.0.1:3080'));
  let status, headers;
  const res = { writeHead(code, values) { status = code; headers = values; }, end() {} };
  const served = auth.authorizeIndex({ method: 'GET', url: launch.pathname + launch.search, headers: { host: launch.host } }, res);
  assert.equal(served, false);
  assert.equal(status, 303);
  return { method: 'GET', url: '/', headers: { host: launch.host, cookie: headers['set-cookie'].split(';', 1)[0] } };
}

test('real upstream browser auth boots before Vault unlock without accessing encrypted storage', async () => {
  const f = lockedProvider();
  const [first, reloaded] = await Promise.all([
    BrowserAuth.create(f.root, f.provider, 30), BrowserAuth.create(f.root, f.provider, 30),
  ]);
  const request = authorize(first);
  assert.equal(reloaded.isAuthenticated(request), true);
  assert.equal(f.accesses(), 0);
  assert.deepEqual(f.ctx.updates, ['client-connection/browser-session']);
  const other = lockedProvider();
  const restarted = await BrowserAuth.create(other.root, other.provider, 30);
  assert.equal(restarted.isAuthenticated(request), false);
});

test('browser bootstrap exception does not unlock model refs or system authorization grants', async () => {
  const f = lockedProvider();
  await BrowserAuth.create(f.root, f.provider, 30);
  await assert.rejects(f.provider.resolve('DEEPSEEK_API_KEY'), { code: 'VAULT_LOCKED' });
  for (const key of ['dataplatform/session', 'forge/session', 'client-connection/another-record']) {
    await assert.rejects(f.provider.readRecord(key), { code: 'VAULT_LOCKED' });
    await assert.rejects(f.provider.modifyRecord(key, () => undefined), { code: 'VAULT_LOCKED' });
  }
  assert.equal(f.accesses(), 7);
  assert.deepEqual(await f.provider.listRecords(), []);
});

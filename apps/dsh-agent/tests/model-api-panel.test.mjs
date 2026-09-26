import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { randomBytes } from 'node:crypto';

async function plugin() {
  const source = await readFile(new URL('../model-api-browser/client.js', import.meta.url), 'utf8');
  let registered;
  runInNewContext(source, {
    window: { __ModuleLoader__: { load: module => { registered = module; } } },
    crypto: { randomUUID: () => randomBytes(16).toString('hex') },
    URL,
  });
  assert.equal(registered.id, '@johnason/dsh-model-api');
  return registered.factory(id => {
    assert.equal(id, 'react');
    return { createElement() {}, useState() {}, useEffect() {}, useRef() {} };
  });
}

function harness({ providers = {}, revision = 4, writable = true, credential = { configured: false, writable: true }, setReply = { ok: true }, mutateReply } = {}) {
  const calls = { set: [], mutate: [], describeCredential: [] };
  const ctx = { remote: {
    settings: {
      describe: async () => ({ ok: true, value: { writable, namespaces: [{ ns: 'llm-pi-ai', value: { providers }, revision }] } }),
      mutate: async (...args) => { calls.mutate.push(args); return mutateReply || { ok: true, value: { ns: 'llm-pi-ai', value: { providers: {} }, revision: revision + 1 } }; },
    },
    credentials: {
      describe: async refs => { calls.describeCredential.push(refs); return { ok: true, value: Object.fromEntries(refs.map(ref => [ref, credential])) }; },
      set: async (...args) => { calls.set.push(args); return setReply; },
    },
  } };
  return { ctx, calls };
}

const input = (key = 'synthetic-' + randomBytes(12).toString('hex')) => ({
  type: 'cloud', slug: 'test-one', displayName: '测试一', baseURL: 'https://models.example.test/v1',
  api: 'openai-completions', modelId: 'model-one', key,
});

test('a save creates one fenced provider path and stores the key only through credentials', async () => {
  const p = await plugin();
  const { ctx, calls } = harness();
  const controller = p.createController(ctx);
  await controller.load();
  const draft = controller.newDraft(input());
  const key = draft.key;
  const result = await controller.save(draft);
  assert.equal(result.ok, true);
  assert.equal(calls.set.length, 1);
  assert.equal(calls.set[0][1], key);
  assert.match(calls.set[0][0], /^[A-Z][A-Z0-9_]*$/);
  assert.equal(calls.mutate.length, 1);
  assert.equal(calls.mutate[0][0], 'llm-pi-ai');
  assert.equal(calls.mutate[0][2], 4);
  assert.deepEqual(Array.from(calls.mutate[0][1][0].path), ['providers', 'johnason-test-one']);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.mutate[0][1][0].value)), {
    displayName: '测试一', api: 'openai-completions', baseURL: 'https://models.example.test/v1',
    models: [{ id: 'model-one', maxTokens: 4096 }], apiKeyEnv: calls.set[0][0],
  });
  assert.equal(JSON.stringify(calls.mutate).includes(key), false);
  assert.equal(draft.key, '');
});

test('duplicate route is refused before any credential write', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ providers: { 'johnason-test-one': { headers: { Authorization: 'private' } } } });
  const controller = p.createController(ctx);
  await controller.load();
  const result = await controller.save(controller.newDraft(input()));
  assert.equal(result.ok, false);
  assert.match(result.message, /已存在/);
  assert.equal(calls.set.length, 0);
  assert.equal(calls.mutate.length, 0);
});

test('revision conflict retains draft ref and prompts refresh without deleting a credential', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ mutateReply: { ok: false, error: { code: 'settings/conflict', message: 'host details' } } });
  const controller = p.createController(ctx);
  await controller.load();
  const draft = controller.newDraft(input());
  const ref = draft.credentialRef;
  const result = await controller.save(draft);
  assert.equal(result.ok, false);
  assert.match(result.message, /刷新.*重试/);
  assert.equal(draft.credentialRef, ref);
  assert.equal(calls.set.length, 1);
  assert.equal(calls.mutate.length, 1);
});

test('an in-flight save uses its original revision even if another load sees that route', async () => {
  const p = await plugin();
  let current = { providers: {}, revision: 4 };
  const { ctx, calls } = harness();
  ctx.remote.settings.describe = async () => ({ ok: true, value: { writable: true, namespaces: [{ ns: 'llm-pi-ai', value: { providers: current.providers }, revision: current.revision }] } });
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  ctx.remote.credentials.describe = async refs => {
    await gate;
    return { ok: true, value: Object.fromEntries(refs.map(ref => [ref, { configured: false, writable: true }])) };
  };
  const controller = p.createController(ctx);
  await controller.load();
  const pending = controller.save(controller.newDraft(input()));
  current = { providers: { 'johnason-test-one': { displayName: 'Other writer' } }, revision: 5 };
  await controller.load();
  release();
  await pending;
  assert.equal(calls.mutate.length, 1);
  assert.equal(calls.mutate[0][2], 4);
});

test('locked Vault blocks saving with an explicit guide and no secret in the error', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ credential: { configured: false, writable: false } });
  const controller = p.createController(ctx);
  await controller.load();
  const draft = controller.newDraft(input());
  const result = await controller.save(draft);
  assert.equal(result.ok, false);
  assert.match(result.message, /Vault.*解锁/);
  assert.equal(result.message.includes(draft.key), false);
  assert.equal(calls.set.length, 0);
});

test('each draft has a separate ref and a keyless local connection gets a Vault placeholder', async () => {
  const p = await plugin();
  const { ctx, calls } = harness();
  const controller = p.createController(ctx);
  await controller.load();
  const first = controller.newDraft({ ...input(''), type: 'local', baseURL: 'http://localhost:8000/v1' });
  const second = controller.newDraft({ ...input(''), type: 'local', baseURL: 'http://localhost:9000/v1' });
  assert.notEqual(first.credentialRef, second.credentialRef);
  assert.equal((await controller.save(first)).ok, true);
  assert.equal(calls.set.length, 1);
  assert.equal(calls.set[0][0], first.credentialRef);
  assert.match(calls.set[0][1], /^[0-9a-f]{32}$/);
  assert.equal(JSON.stringify(calls.mutate).includes(calls.set[0][1]), false);
});

test('configured list exposes only safe fields and a locked Vault state', async () => {
  const p = await plugin();
  const { ctx } = harness({
    providers: { 'johnason-test-one': {
      displayName: '测试一', api: 'openai-completions', models: [{ id: 'model-one' }],
      apiKeyEnv: 'JOHNASON_MODEL_API_REF', headers: { Authorization: 'synthetic-secret' },
    } },
    credential: { configured: false, writable: false },
  });
  const view = await p.createController(ctx).load();
  assert.deepEqual(JSON.parse(JSON.stringify(view.rows)), [{
    route: 'johnason-test-one', displayName: '测试一', api: 'openai-completions',
    models: ['model-one'], credentialStatus: 'Vault 已锁定，请前往 Vault 解锁',
  }]);
  assert.equal(JSON.stringify(view).includes('synthetic-secret'), false);
});

test('credential status reads are batched within the 64-reference Remote limit', async () => {
  const p = await plugin();
  const providers = Object.fromEntries(Array.from({ length: 65 }, (_, i) => [
    'johnason-provider-' + i,
    { displayName: 'Provider ' + i, apiKeyEnv: 'JOHNASON_MODEL_API_REF_' + i },
  ]));
  const { ctx } = harness({ providers });
  const batches = [];
  ctx.remote.credentials.describe = async refs => {
    batches.push(refs);
    if (refs.length > 64) return { ok: false, error: { code: 'too-many' } };
    return { ok: true, value: Object.fromEntries(refs.map(ref => [ref, { configured: true, writable: true }])) };
  };
  const view = await p.createController(ctx).load();
  assert.equal(view.rows.length, 65);
  assert.deepEqual(batches.map(batch => batch.length), [64, 1]);
  assert.equal(view.rows.every(row => row.credentialStatus === '凭据已配置'), true);
});

test('URL policy permits only loopback HTTP local and HTTPS cloud without URL extras', async () => {
  const p = await plugin();
  for (const url of ['http://localhost:8000/v1', 'http://127.0.0.1:8000/v1', 'http://[::1]:8000/v1', 'https://localhost/v1']) {
    assert.equal(p.validateURL(url, 'local'), true, url);
  }
  for (const url of ['http://example.com/v1', 'http://127.0.0.2/v1', 'http://127.1/v1', 'http://2130706433/v1', 'https://user:pass@localhost/v1', 'http://localhost/v1?x=1', 'http://localhost/v1#x', 'http://localhost/v1?', 'http://localhost/v1#']) {
    assert.equal(p.validateURL(url, 'local'), false, url);
  }
  assert.equal(p.validateURL('https://api.example.test/v1', 'cloud'), true);
  for (const url of ['http://api.example.test/v1', 'https://user@api.example.test/v1', 'https://@api.example.test/v1', 'https://api.example.test/v1?q=x']) {
    assert.equal(p.validateURL(url, 'cloud'), false, url);
  }
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const defaults = {
  enabled: true, baseURL: 'http://127.0.0.1:1234/v1', apiKeyRef: '',
  decisionModel: 'jev-style-qwen3.5-2b-decision', workerModel: 'qwen3.8-27b-uncensored-mlx',
  timeoutMs: 60000, maxTokens: 2048, threshold: 0.75, maxCallsPerTurn: 256, maxConcurrent: 2,
};

async function plugin(fetcher = async () => { throw new Error('unexpected fetch'); }, react) {
  const source = await readFile(new URL('../local-model-browser/client.js', import.meta.url), 'utf8');
  let registered;
  let refSequence = 0;
  runInNewContext(source, {
    window: { __ModuleLoader__: { load: module => { registered = module; } } },
    crypto: { randomUUID: () => '11111111-2222-3333-4444-' + String(++refSequence).padStart(12, '0') },
    URL, fetch: fetcher,
  });
  assert.equal(registered.id, '@johnason/dsh-local-models');
  return registered.factory(id => {
    assert.equal(id, 'react');
    return react || { createElement() {}, useState() {}, useEffect() {}, useRef() {} };
  });
}

function harness({ value = defaults, revision = 9, writable = true, credential = { configured: true, writable: true }, mutateReply = { ok: true }, setReply = { ok: true } } = {}) {
  const calls = { mutate: [], describeCredential: [], set: [] };
  const ctx = { remote: {
    settings: {
      describe: async () => ({ ok: true, value: { writable, namespaces: [{ ns: 'johnason-local-models', value, revision }] } }),
      mutate: async (...args) => { calls.mutate.push(args); return mutateReply; },
    },
    credentials: {
      describe: async refs => { calls.describeCredential.push(refs); return { ok: true, value: Object.fromEntries(refs.map(ref => [ref, credential])) }; },
      set: async (...args) => { calls.set.push(args); return setReply; },
    },
  } };
  return { ctx, calls };
}

function mutations(calls) {
  return Object.fromEntries(calls.mutate[0][1].map(op => [op.path[0], op.value]));
}

test('load projects only editable values and credential status', async () => {
  const p = await plugin();
  const { ctx } = harness({ value: { ...defaults, apiKeyRef: 'SECRET_REF', privateInstruction: 'do not expose' } });
  const view = await p.createController(ctx).load();
  assert.equal(view.revision, 9);
  assert.equal(view.draft.baseURL, defaults.baseURL);
  assert.equal(view.draft.apiKey, '');
  assert.equal(view.hasCredential, true);
  assert.equal(JSON.stringify(view).includes('SECRET_REF'), false);
  assert.equal(JSON.stringify(view).includes('privateInstruction'), false);
});

test('keyless local save needs no Vault access and writes only schema fields with CAS', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ value: { ...defaults, unrelated: 'keep' } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  view.draft.workerModel = 'new-worker';
  const result = await controller.save(view.draft);
  assert.equal(result.ok, true);
  assert.equal(calls.describeCredential.length, 0);
  assert.equal(calls.set.length, 0);
  assert.equal(calls.mutate[0][0], 'johnason-local-models');
  assert.equal(calls.mutate[0][2], 9);
  assert.deepEqual(Object.keys(mutations(calls)).sort(), Object.keys(defaults).sort());
  assert.equal(mutations(calls).workerModel, 'new-worker');
  assert.equal(mutations(calls).apiKeyRef, '');
  assert.equal(JSON.stringify(calls.mutate).includes('unrelated'), false);
});

test('same URL retains an existing credential only after checking Vault writability', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ value: { ...defaults, apiKeyRef: 'JOHNASON_LOCAL_OLD' } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  assert.equal((await controller.save(view.draft)).ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.describeCredential)), [['JOHNASON_LOCAL_OLD']]);
  assert.equal(mutations(calls).apiKeyRef, 'JOHNASON_LOCAL_OLD');
  assert.equal(calls.set.length, 0);
});

test('URL changes clear the old credential and never forward it', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ value: { ...defaults, apiKeyRef: 'JOHNASON_LOCAL_OLD' } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  view.draft.baseURL = 'http://localhost:5678/v1';
  assert.equal((await controller.save(view.draft)).ok, true);
  assert.equal(mutations(calls).apiKeyRef, '');
  assert.equal(calls.describeCredential.length, 0);
  assert.equal(calls.set.length, 0);
});

test('changing the URL abandons a failed draft key and its credential reference', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ mutateReply: { ok: false } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  view.draft.apiKey = 'synthetic-only-key';
  assert.equal((await controller.save(view.draft)).ok, false);
  const failedRef = view.draft.credentialRef;
  const changed = controller.changeDraft(view.draft, 'baseURL', 'http://localhost:5678/v1');
  assert.equal(changed.apiKey, '');
  assert.notEqual(changed.credentialRef, failedRef);
  assert.equal(changed.baseURL, 'http://localhost:5678/v1');
  await controller.save(changed);
  assert.equal(calls.set.length, 1);
  assert.equal(mutations({ mutate: [calls.mutate[1]] }).apiKeyRef, '');
});

test('an API key goes only to Vault and a failed CAS keeps draft and stable ref', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ mutateReply: { ok: false, error: { message: 'secret-details' } } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  view.draft.apiKey = 'synthetic-only-key';
  const first = await controller.save(view.draft);
  assert.equal(first.ok, false);
  assert.match(first.message, /刷新/);
  assert.equal(first.message.includes('secret-details'), false);
  assert.equal(calls.set[0][1], 'synthetic-only-key');
  const ref = calls.set[0][0];
  assert.match(ref, /^[A-Z][A-Z0-9_]*$/);
  assert.equal(mutations(calls).apiKeyRef, ref);
  assert.equal(JSON.stringify(calls.mutate).includes('synthetic-only-key'), false);
  await controller.save(view.draft);
  assert.equal(calls.set[1][0], ref);
  assert.equal(view.draft.apiKey, 'synthetic-only-key');
});

test('no-auth clears a reference without deleting the Vault record', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ value: { ...defaults, apiKeyRef: 'JOHNASON_LOCAL_OLD' } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  view.draft.noAuth = true;
  assert.equal((await controller.save(view.draft)).ok, true);
  assert.equal(mutations(calls).apiKeyRef, '');
  assert.equal(calls.describeCredential.length, 0);
  assert.equal(calls.set.length, 0);
});

test('invalid URL, bounds, and locked Vault stop before settings mutation', async () => {
  const p = await plugin();
  for (const patch of [{ baseURL: 'http://example.com/v1' }, { threshold: 1.1 }, { timeoutMs: 999 }, { maxTokens: 63 }, { maxCallsPerTurn: 2001 }, { maxConcurrent: 5 }]) {
    const { ctx, calls } = harness();
    const controller = p.createController(ctx);
    const view = await controller.load();
    assert.equal((await controller.save({ ...view.draft, ...patch })).ok, false);
    assert.equal(calls.mutate.length, 0);
  }
  const { ctx, calls } = harness({ credential: { configured: false, writable: false } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  view.draft.apiKey = 'synthetic-key';
  const result = await controller.save(view.draft);
  assert.equal(result.ok, false);
  assert.match(result.message, /Vault.*解锁/);
  assert.equal(calls.set.length, 0);
  assert.equal(calls.mutate.length, 0);
});

test('large decision budgets can be saved without changing credentials or concurrency', async () => {
  const p = await plugin();
  const { ctx, calls } = harness({ value: { ...defaults, maxCallsPerTurn: 8 } });
  const controller = p.createController(ctx);
  const view = await controller.load();
  for (const quota of [200, 256, 2000]) {
    assert.equal((await controller.save({ ...view.draft, maxCallsPerTurn: quota })).ok, true);
    const values = mutations({ mutate: [calls.mutate.at(-1)] });
    assert.equal(values.maxCallsPerTurn, quota);
    assert.equal(values.maxConcurrent, 2);
    assert.equal(values.baseURL, defaults.baseURL);
  }
  assert.equal(calls.set.length, 0);
  assert.equal(controller.newDraft().maxCallsPerTurn, 256);
});

test('model list uses only the same-origin GET and sanitizes remote errors', async () => {
  const requests = [];
  const p = await plugin(async (...args) => { requests.push(args); return { ok: true, json: async () => ({ models: [{ id: 'model-a' }, { id: 'model-b' }], decisionModel: 'model-a', workerModel: 'model-b' }) }; });
  const { ctx } = harness();
  const controller = p.createController(ctx);
  await controller.load();
  const result = await controller.listModels();
  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.models)), [{ id: 'model-a' }, { id: 'model-b' }]);
  assert.equal(requests[0][0], '/johnason/local-models/models');
  assert.equal(requests[0][1].method, 'GET');
  assert.equal(requests[0][1].credentials, 'same-origin');
  const bad = await plugin(async () => ({ ok: false, json: async () => ({ code: 'secret-detail', message: 'credential-leak' }) }));
  const failure = await bad.createController(harness().ctx).listModels();
  assert.equal(failure.ok, false);
  assert.equal(JSON.stringify(failure).includes('credential-leak'), false);
});

test('panel exposes the specified controls in the plugin configuration slot', async () => {
  const react = {
    createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
    useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}],
    useEffect: () => {},
    useRef: initial => ({ current: initial }),
  };
  const p = await plugin(undefined, react);
  let render;
  p.apply({ remote: harness().ctx.remote, slots: {
    inject: (name, register) => { assert.equal(name, 'plugins.bundle.config'); register(); },
    register: (metadata, callback) => { assert.equal(metadata.key, '@johnason/dsh-local-models'); render = callback; },
  } });
  assert.equal(render({ view: 'summary' }), '本地模型与 Jev 混合调用');
  const element = render({ view: 'detail' });
  const tree = element.type(element.props);
  const all = [];
  function visit(node) {
    if (!node || typeof node !== 'object') return;
    all.push(node);
    node.children?.forEach(visit);
  }
  visit(tree);
  assert.equal(tree.props['data-johnason-local-models'], true);
  assert.deepEqual(all.filter(node => node.type === 'input').map(node => node.props['aria-label']), [
    '启用本地模型工具', 'LM Studio API 地址', 'Jev 决策模型', '本地工作模型',
    'LM Studio API Key', '无需认证', '决策阈值', '调用超时（毫秒）', '输出上限',
    '每轮调用预算', '并发上限',
  ]);
  assert.deepEqual(all.filter(node => node.type === 'button').map(node => node.children[0]), [
    '保存混合配置', '刷新配置', '读取本地模型',
  ]);
  assert.equal(all.find(node => node.type === 'input' && node.props['aria-label'] === 'LM Studio API 地址').props.disabled, true,
    'initial settings load must finish before the user can edit a draft');
  const vault = all.find(node => node.type === 'a' && node.props.href === '/vault');
  assert.equal(all.find(node => node.type === 'input' && node.props['aria-label'] === '每轮调用预算').props.max, 2000);
  assert.equal(vault.props.target, '_blank');
  assert.match(vault.props.rel, /noopener/);
});

test('a delayed first settings load keeps editing locked until the loaded draft is ready', async () => {
  const { ctx } = harness();
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const describe = ctx.remote.settings.describe;
  ctx.remote.settings.describe = async () => { await gate; return describe(); };
  const states = [];
  let cursor = 0;
  let effect;
  const react = {
    createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
    useState: initial => {
      const index = cursor++;
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
    },
    useEffect: callback => { effect = callback; },
    useRef: initial => {
      const index = cursor++;
      if (!(index in states)) states[index] = { current: initial };
      return states[index];
    },
  };
  const p = await plugin(undefined, react);
  let panel;
  p.apply({ remote: ctx.remote, slots: {
    inject: (_name, register) => register(),
    register: (_metadata, render) => { panel = render; },
  } });
  const render = () => { cursor = 0; const element = panel({ view: 'detail' }); return element.type(element.props); };
  const find = (node, predicate) => {
    if (!node || typeof node !== 'object') return undefined;
    if (predicate(node)) return node;
    for (const child of node.children ?? []) {
      const match = find(child, predicate);
      if (match) return match;
    }
  };
  const address = tree => find(tree, node => node.type === 'input' && node.props['aria-label'] === 'LM Studio API 地址');
  let tree = render();
  assert.equal(address(tree).props.disabled, true);
  effect();
  tree = render();
  address(tree).props.onChange({ target: { value: 'http://localhost:5678/v1' } });
  release();
  await new Promise(resolve => setImmediate(resolve));
  tree = render();
  assert.equal(address(tree).props.disabled, false);
  assert.equal(address(tree).props.value, defaults.baseURL);
  address(tree).props.onChange({ target: { value: 'http://localhost:5678/v1' } });
  assert.equal(address(render()).props.value, 'http://localhost:5678/v1');
});

test('host bundle delegates to its configured implementation with settings', async () => {
  const host = await import('../local-model-browser/index.mjs');
  assert.deepEqual(host.inject, ['settings', 'tools', 'credentials']);
  const implementation = 'data:text/javascript,export function apply(ctx,settings){return {ctx,settings}}';
  const ctx = { marker: 'host' };
  assert.deepEqual(await host.apply(ctx, { implementation, settings: { enabled: false } }), {
    ctx, settings: { enabled: false },
  });
  await assert.rejects(() => host.apply(ctx, {}), /路径/);
});

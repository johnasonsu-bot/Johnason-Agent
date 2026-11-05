import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ecosystemOverlay, ecosystemPlan, runEcosystemPlan, subscriptionRuntimeDigest } from '../src/ecosystem.mjs';

test('ecosystem keeps remote bridges off and configures only a local Ollama route', () => {
  const rows = ecosystemOverlay(['gemma4:e4b']);
  for (const id of ['web-ui-remote-bridge', 'web-ui-task-board-remote', 'web-ui-ssh', 'web-ui-market']) {
    assert.equal(rows.find(row => row.id === id).disabled, true);
  }
  const provider = rows.find(row => row.id === 'llm-pi-ai').config.providers['johnason-ollama'];
  assert.equal(provider.baseURL, 'http://127.0.0.1:11434/v1');
  assert.equal(provider.models[0].id, 'gemma4:e4b');
  assert.equal(Object.hasOwn(provider, 'apiKey'), false);
  assert.equal(provider.apiKeyEnv, 'JOHNASON_OLLAMA_LOCAL_PLACEHOLDER');
  assert.equal(rows.find(row => row.id === 'credentials').config.localOllama, true);
});

test('model names are validated and deduplicated before becoming config', () => {
  assert.throws(() => ecosystemOverlay([]), /model/i);
  assert.throws(() => ecosystemOverlay(['bad\nname']), /model/i);
  assert.equal(ecosystemOverlay(['gemma4:e4b', 'gemma4:e4b']).find(row => row.id === 'llm-pi-ai').config.providers['johnason-ollama'].models.length, 1);
});

test('installation pins community versions, stages secure subscriptions and uses isolated data', () => {
  const plan = ecosystemPlan({ repoRoot: '/work/repo', dataRoot: '/private/new-profile', workspace: '/work/repo' });
  assert.ok(plan.commands.some(args => args.includes('@linxin666/dsh-web-all@0.4.15')));
  assert.ok(plan.commands.some(args => args.includes('dshmarket@1.73.11')));
  assert.ok(plan.commands.some(args => args.some(value => value === 'file:/private/new-profile/packages/subscriptions-unbuilt')));
  assert.ok(plan.commands.some(args => args[0].endsWith('/stage-runtime.mjs')));
  assert.ok(plan.commands.every(args => !args.some(value => value.startsWith('link:'))));
  assert.ok(plan.commands.filter(args => args[0].endsWith('/cli.mjs')).every(args => args.includes('/private/new-profile')));
  assert.ok(plan.commands.filter(args => args.includes('add')).every(args => args.includes('--ignore-scripts')));
  assert.ok(plan.launchArgs.includes('--no-open'));
  assert.ok(plan.launchArgs.includes('--patch'));
});

test('runtime digest ignores development metadata but detects shipped code changes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-runtime-digest-'));
  await mkdir(join(root, 'lib'));
  await mkdir(join(root, 'native'));
  for (const name of ['lib/index.js', 'cordis.patch.yml', 'LICENSE', 'LOCAL-ADAPTER.md', 'UPSTREAM.json', 'native/keychain-helper', 'native/keychain-helper.c']) await writeFile(join(root, name), name);
  await writeFile(join(root, 'package.json'), JSON.stringify({ name: 'test', private: true, devDependencies: { oldPeer: '1' }, scripts: { build: 'test' } }));
  const first = await subscriptionRuntimeDigest(root);
  await writeFile(join(root, 'package.json'), JSON.stringify({ name: 'test', private: true }));
  assert.equal(await subscriptionRuntimeDigest(root), first);
  await writeFile(join(root, 'lib/index.js'), 'changed runtime');
  assert.notEqual(await subscriptionRuntimeDigest(root), first);
});

test('inspection has no subprocess side effects', async () => {
  let calls = 0;
  await runEcosystemPlan({ commands: [['first']] }, { apply: false, run: async () => { calls++; return 0; } });
  assert.equal(calls, 0);
});

test('a failed install stops before subsequent installs or validation', async () => {
  const calls = [];
  await assert.rejects(runEcosystemPlan({ commands: [['first'], ['second'], ['third']] }, {
    apply: true, run: async args => { calls.push(args[0]); return args[0] === 'second' ? 7 : 0; },
  }), /step 2.*7/i);
  assert.deepEqual(calls, ['first', 'second']);
});

test('success runs every step sequentially', async () => {
  const calls = [];
  const result = await runEcosystemPlan({ commands: [['first'], ['second']] }, {
    apply: true, run: async args => { calls.push(args[0]); return 0; },
  });
  assert.equal(result.status, 'installed');
  assert.deepEqual(calls, ['first', 'second']);
});

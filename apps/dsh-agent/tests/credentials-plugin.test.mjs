import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import Provider from '../src/credentials-plugin.mjs';
const require = createRequire(new URL('../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url));
const { Context } = await import(require.resolve('@deepseek-ai/cordis'));

test('locked resolution fails; changes are fresh and descriptions never reveal values', async () => {
  const ctx = new Context();
  const provider = new Provider(ctx, { path: join(await mkdtemp(join(tmpdir(), 'dsh-provider-')), 'vault.enc'), mode: 'web' });
  const events = [];
  const recordEvents = [];
  ctx.on('credentials/reference-updated', ref => events.push(ref));
  ctx.on('credentials/record-updated', key => recordEvents.push(key));
  await assert.rejects(provider.resolve('DEEPSEEK_API_KEY'), { code: 'VAULT_LOCKED' });
  assert.deepEqual(await provider.describe('DEEPSEEK_API_KEY'), { configured: false, writable: false });
  await provider.vault.initialize('test-password');
  const previous = process.env.DEEPSEEK_API_KEY;
  process.env.DEEPSEEK_API_KEY = 'synthetic-env-value';
  try { assert.equal(await provider.resolve('DEEPSEEK_API_KEY'), undefined); }
  finally { if (previous === undefined) delete process.env.DEEPSEEK_API_KEY; else process.env.DEEPSEEK_API_KEY = previous; }
  await provider.set('DEEPSEEK_API_KEY', 'unit-value');
  assert.deepEqual(await provider.describe('DEEPSEEK_API_KEY'), { configured: true, source: 'vault', writable: true });
  assert.equal((await provider.resolve('DEEPSEEK_API_KEY')).value, 'unit-value');
  await provider.set('DEEPSEEK_API_KEY', 'new-unit-value');
  assert.equal((await provider.resolve('DEEPSEEK_API_KEY')).value, 'new-unit-value');
  await provider.modifyRecord('llm-test/provider', async () => ({ kind: 'api-key', key: 'unit-value' }));
  assert.equal(JSON.stringify(await provider.listRecords()).includes('unit-value'), false);
  assert.deepEqual(await provider.describeRecord('llm-test/provider'), { configured: true, kind: 'api-key', writable: true });
  assert.deepEqual(await provider.modifyRecord('llm-test/provider', async () => undefined), { kind: 'api-key', key: 'unit-value' });
  await assert.rejects(provider.modifyRecord('llm-test/provider', async () => ({ kind: 'grant', payload: NaN })), /JSON/);
  await assert.rejects(provider.modifyRecord('llm-test/provider', async () => ({ kind: 'api-key', key: '' })), /record/);
  await provider.deleteRecord('llm-test/provider');
  await provider.unset('DEEPSEEK_API_KEY');
  assert.equal(await provider.resolve('DEEPSEEK_API_KEY'), undefined);
  assert.equal(events.length, 3);
  assert.deepEqual(recordEvents, ['llm-test/provider', 'llm-test/provider']);
  provider.vault.lock();
  await ctx.fiber.dispose();
});

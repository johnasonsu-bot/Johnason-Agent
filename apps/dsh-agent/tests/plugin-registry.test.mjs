import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const registryUrl = new URL('../src/plugins/index.mjs', import.meta.url);
test('local plugin plan declares protected credentials, optional systems and Web assets', async () => {
  const registry = await import(registryUrl).catch(() => ({}));
  assert.equal(typeof registry.createPluginPlan, 'function');
  const plan = registry.createPluginPlan({ dataRoot: '/isolated/data', mode: 'web', providers: [] });
  const overlay = plan.profileOverlay({ authorizationEntry: '/native/authorization.mjs' });
  assert.equal(plan.contractVersion, 1);
  assert.equal(plan.credential.entry, fileURLToPath(new URL('../src/credentials-plugin.mjs', import.meta.url)), 'persisted profile name guards retain their public entry');
  assert.equal(plan.credential.implementationEntry, fileURLToPath(new URL('../src/plugins/credentials/credentials-plugin.mjs', import.meta.url)));
  assert.deepEqual(overlay[0], { id: 'credentials', config: { path: '/isolated/data/vault.enc', mode: 'web' } });
  assert.equal(overlay.flatMap(row => row.insert ?? []).filter(row => row.id === 'memory-recovery').length, 1);
  assert.equal(overlay.flatMap(row => row.insert ?? []).find(row => row.id === 'memory-recovery').name, fileURLToPath(new URL('../src/memory-recovery-plugin.mjs', import.meta.url)));
  assert.equal(overlay.flatMap(row => row.insert ?? []).some(row => row.id === 'dataplatform'), false);
  assert.equal(plan.clientBundles.length, 1);
  assert.equal(registry.createPluginPlan({ dataRoot: '/isolated/data', mode: 'headless', providers: [] }).clientBundles.length, 0);
  const systems = registry.createPluginPlan({ dataRoot: '/isolated/data', mode: 'web', providers: [
    { id: 'dataplatform', label: 'Data Platform', type: 'dataplatform', url: 'http://127.0.0.1:46120', corePath: '/trusted/core.cjs' },
  ] });
  const inserted = systems.profileOverlay({ authorizationEntry: '/native/authorization.mjs' }).flatMap(row => row.insert ?? []);
  assert.ok(inserted.some(row => row.id === 'dataplatform-authorization' && row.name === '/native/authorization.mjs'));
  assert.equal(inserted.find(row => row.id === 'dataplatform').config.providers[0].id, 'dataplatform');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, realpath, mkdir, copyFile, rename, symlink, lstat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { prepareProfile } from '../src/profile.mjs';
import { createPluginPlan } from '../src/plugins/index.mjs';

for (const [name, id] of [['@johnason/dsh-memory-ui', 'memory-ui'], ['@johnason/dsh-model-api', 'model-api'], ['@johnason/dsh-local-models', 'local-models']]) {
test(`${name} is installed without duplicate activation or overriding an explicit disable`, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-local-bundle-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const launch = { dataRoot, upstreamRoot, mode: 'web', args: ['web', '--no-open'] };
  const first = await prepareProfile(launch);
  const manifestPath = join(dataRoot, 'profiles/web/package.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  assert.ok(manifest.dependencies?.[name], 'native Installed list requires a profile dependency');
  assert.ok(manifest.dsh.profile.bundles.includes(name));
  assert.equal(first.rows.filter(row => row.id === id).length, 1);
  const bundle = JSON.parse(await readFile(join(dataRoot, 'profiles/web/node_modules', name, 'package.json'), 'utf8'));
  assert.ok(bundle.version);
  assert.ok(bundle.dsh.bundle.patch);
  const repeated = await prepareProfile(launch);
  assert.equal(repeated.rows.filter(row => row.id === id).length, 1);
  // A user can hide the local UI without changing host credentials or session policy.
  manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.filter(value => value !== name);
  await writeFile(manifestPath, JSON.stringify(manifest));
  const disabled = await prepareProfile(launch);
  assert.equal(disabled.rows.some(row => row.id === id), false);
  assert.equal(disabled.rows.filter(row => row.id === 'credentials').length, 1);
  assert.ok(disabled.rows.some(row => row.id === 'memory-recovery'));
});
}

test('disabled local UI retains safe provider configuration for a native live re-enable', async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-bundle-reactivate-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const launch = { dataRoot, upstreamRoot, mode: 'web', args: ['web'], providers: [
    { id: 'forge', label: 'Forge', type: 'forge', url: 'http://127.0.0.1:8010', python: '/trusted/python', sourceRoot: '/trusted/forge', dbPath: '/trusted/data.db' },
  ] };
  await prepareProfile(launch);
  const manifestPath = join(dataRoot, 'profiles/web/package.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.filter(name => name !== '@johnason/dsh-memory-ui');
  await writeFile(manifestPath, JSON.stringify(manifest));
  const disabled = await prepareProfile(launch);
  assert.equal(disabled.rows.some(row => row.id === 'memory-ui'), false);
  const overlay = JSON.parse(await readFile(disabled.overlayPath, 'utf8'));
  const require = createRequire(join(upstreamRoot, 'apps/cli/package.json'));
  const boot = await import(require.resolve('@deepseek-ai/dsh-app-boot'));
  const reenabled = boot.composeEntries([[{ insert: [{ id: 'memory-ui', name: '@johnason/dsh-memory-ui' }] }], overlay]);
  assert.deepEqual(reenabled.find(row => row.id === 'memory-ui').config?.providers, [{ id: 'forge', label: 'Forge', type: 'forge' }]);
});

for (const [name, id] of [['@johnason/dsh-memory-ui', 'memory-ui'], ['@johnason/dsh-model-api', 'model-api'], ['@johnason/dsh-local-models', 'local-models']]) {
test(`${name} uninstall is not undone by a later launcher run`, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-bundle-uninstall-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const launch = { dataRoot, upstreamRoot, mode: 'web', args: ['web'] };
  await prepareProfile(launch);
  const manifestPath = join(dataRoot, 'profiles/web/package.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  // Emulate the native manager's manifest update, retaining files per user policy.
  delete manifest.dependencies[name];
  manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.filter(value => value !== name);
  await writeFile(manifestPath, JSON.stringify(manifest));
  const restarted = await prepareProfile(launch);
  assert.equal(restarted.rows.some(row => row.id === id), false);
  assert.equal(Object.hasOwn(JSON.parse(await readFile(manifestPath, 'utf8')).dependencies, name), false);
});
}

test('native bundles survive encrypted overlay and duplicate providers fail closed', async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-profile-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const launch = { dataRoot, upstreamRoot, mode: 'web', args: ['web', '--no-open'] };
  const prepared = await prepareProfile(launch);
  assert.equal(prepared.profile, 'web');
  assert.deepEqual(prepared.args, ['--no-open']);
  assert.equal(prepared.rows.filter(r => r.id === 'credentials').length, 1);
  assert.equal(prepared.rows.find(r => r.id === 'credentials').name,
    new URL('../src/credentials-plugin.mjs', import.meta.url).href);
  assert.equal(prepared.rows.some(r => r.name?.includes('credentials-local')), false);
  const require = createRequire(join(upstreamRoot, 'apps/cli/package.json'));
  const boot = await import(require.resolve('@deepseek-ai/dsh-app-boot'));
  const native = boot.loadOverlayPatches('test', join(upstreamRoot, 'packages/bundle/base/cordis.patch.yml'));
  const generated = boot.loadOverlayPatches('test', join(dataRoot, 'profiles/node_modules/@johnason/dsh-encrypted-base/cordis.patch.yml'));
  const withoutCredential = patches => patches[0].insert.filter(row => row.id !== 'credentials');
  assert.deepEqual(withoutCredential(generated), withoutCredential(native));
  for (const id of ['ui-skill', 'ui-subagent', 'ui-plan', 'ui-goal', 'web-runtime']) assert.ok(prepared.rows.some(r => r.id === id), id);
  const memory = prepared.rows.find(row => row.id === 'memory-recovery');
  assert.equal(memory?.name, fileURLToPath(new URL('../src/memory-recovery-plugin.mjs', import.meta.url)));
  assert.deepEqual(memory.config, { path: join(dataRoot, 'memory/recovery.sqlite'), enabled: true });
  // Native excludes arbitrary packages from the shared profiles/node_modules.
  const profileRequire = createRequire(join(dataRoot, 'profiles/web/package.json'));
  assert.equal(profileRequire.resolve('@johnason/dsh-memory-ui'),
    await realpath(join(dataRoot, 'profiles/web/node_modules/@johnason/dsh-memory-ui/index.mjs')));
  const patchPath = join(dataRoot, 'profiles/web/cordis.patch.yml');
  await writeFile(patchPath, JSON.stringify([{ insert: [{ id: 'bad-extra', name: '@deepseek-ai/dsh-credentials-local' }] }]));
  await assert.rejects(prepareProfile(launch), /exactly one|credential provider/);
  assert.match(await readFile(patchPath, 'utf8'), /bad-extra/);
  await writeFile(patchPath, JSON.stringify([{ id: 'credentials', disabled: true }]));
  await assert.rejects(prepareProfile(launch), /exactly one|credential provider/i);
});

test('headless keeps the encrypted credential provider while honoring an explicit user disable', async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-headless-credentials-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const launch = { dataRoot, upstreamRoot, mode: 'headless', args: ['--profile', 'headless', 'hello'] };
  const prepared = await prepareProfile(launch);
  const providers = prepared.rows.filter(row => row.id === 'credentials');
  assert.equal(providers.length, 1);
  assert.equal(providers[0].disabled, false);
  const emitted = JSON.parse(await readFile(prepared.overlayPath, 'utf8'));
  assert.ok(emitted.some(row => row.id === 'credentials' && row.disabled === false));
  await writeFile(join(dataRoot, 'profiles/headless/cordis.patch.yml'), JSON.stringify([{ id: 'credentials', disabled: true }]));
  await assert.rejects(prepareProfile(launch), /Exactly one encrypted credential provider/);
});

test('local model implementation stays inherited so native settings remain writable', async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-live-settings-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const prepared = await prepareProfile({ dataRoot, upstreamRoot, mode: 'web', args: ['web'] });
  assert.equal(JSON.parse(await readFile(prepared.overlayPath, 'utf8')).some(row => row.id === 'local-models'), false);
  assert.equal(prepared.rows.find(row => row.id === 'local-models').config.implementation,
    fileURLToPath(new URL('../src/plugins/local-models/plugin.mjs', import.meta.url)));
});


test('native package links cannot make generated bundle patches write through to plugin source', async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-linked-bundle-'));
  const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
  const launch = { dataRoot, upstreamRoot, mode: 'web', args: ['web'] };
  const plugins = createPluginPlan(launch);
  const original = plugins.clientBundles.find(bundle => bundle.name === '@johnason/dsh-local-models');
  const source = join(dataRoot, 'fixture-source');
  await mkdir(source);
  for (const file of original.files) await copyFile(new URL(file, original.sourceRoot), join(source, file));
  // Native plugin add may replace a copied package directory with a source link.
  const sourceRoot = pathToFileURL(source + '/');
  const plan = { ...plugins, clientBundles: plugins.clientBundles.map(bundle => bundle === original ? { ...bundle, sourceRoot } : bundle) };
  const pristine = await readFile(join(source, 'cordis.patch.yml'), 'utf8');
  await prepareProfile(launch, { plugins: plan });
  const generated = join(dataRoot, 'profiles/web/node_modules', original.name);
  await rename(generated, join(dataRoot, 'first-generated-copy'));
  await symlink(source, generated, 'dir');
  const prepared = await prepareProfile(launch, { plugins: plan });
  assert.equal(await readFile(join(source, 'cordis.patch.yml'), 'utf8'), pristine, 'plugin source must stay byte-for-byte unchanged');
  assert.equal((await lstat(generated)).isSymbolicLink(), false, 'generated package must be a profile-owned copy');
  const patch = JSON.parse(await readFile(join(generated, 'cordis.patch.yml'), 'utf8'));
  assert.equal(patch.filter(row => row.id === 'local-models').length, 1);
  assert.equal(prepared.rows.find(row => row.id === 'local-models').config.implementation, original.bundlePatches[0].config.implementation);
  await prepareProfile(launch, { plugins: plan });
  assert.equal(await readFile(join(source, 'cordis.patch.yml'), 'utf8'), pristine);
  assert.deepEqual(JSON.parse(await readFile(join(generated, 'cordis.patch.yml'), 'utf8')), patch, 'restarts cannot accumulate generated patches');
});

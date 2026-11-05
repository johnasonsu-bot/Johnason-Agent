import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const runner = await import('../src/native-runner.mjs').catch(() => ({}));
const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));

function fixture(mode = 'web', failure) {
  const calls = [];
  const config = { mode, upstreamRoot, dataRoot: '/isolated/data', args: [] };
  const plugins = { async prepare() { calls.push('prepare'); if (failure === 'prepare') throw Error('fixture failure'); },
    async dispose() { calls.push('dispose'); } };
  const platform = {
    parseArgs() { return { mode: mode === 'dump-config' ? 'dump-config' : 'profile', profile: 'custom', args: [] }; },
    async environment() { calls.push('environment'); return 'snapshot'; },
    async runPlugin() { await Promise.resolve(); calls.push('plugin'); return 17; },
    async runDumpConfig() { calls.push('dump'); },
    async runProfile(options) { calls.push('run'); assert.equal(options.environment, 'snapshot');
      if (failure === 'run') throw Error('fixture failure');
      return { ctx: { get: () => undefined } }; },
  };
  const prepareProfile = async () => ({ profile: 'custom', args: [], patchFiles: [] });
  return { config, calls, dependencies: { platform, plugins, prepareProfile } };
}

test('native plugin commands await completion and dispose local pre-launch resources', async () => {
  assert.equal(typeof runner.runNative, 'function');
  const f = fixture('plugin');
  assert.equal(await runner.runNative(f.config, f.dependencies), 17);
  assert.deepEqual(f.calls, ['prepare', 'plugin', 'dispose']);
});

test('plugin preparation precedes native environment capture and failure releases resources', async () => {
  assert.equal(typeof runner.runNative, 'function');
  const f = fixture();
  await runner.runNative(f.config, f.dependencies);
  assert.deepEqual(f.calls, ['prepare', 'environment', 'run', 'dispose']);
  for (const failure of ['prepare', 'run']) {
    const failed = fixture('web', failure);
    await assert.rejects(runner.runNative(failed.config, failed.dependencies), /fixture failure/);
    assert.equal(failed.calls.at(-1), 'dispose');
  }
  const dump = fixture('dump-config');
  await runner.runNative(dump.config, dump.dependencies);
  assert.deepEqual(dump.calls, ['dump']);
});

test('native version reporting uses the selected upstream CLI manifest', async () => {
  const { loadLaunchPlatform } = await import('../src/platform/launch-platform.mjs');
  const argsUrl = new URL('../../../third_party/deepseek-harness/apps/cli/src/args.ts', import.meta.url).href;
  const hook = registerHooks({ resolve(specifier, context, next) {
    if (String(specifier) === argsUrl) return { url: 'data:text/javascript,export const parseDshArgs = (args, version) => ({args, version});', shortCircuit: true };
    return next(specifier, context);
  } });
  try {
    const platform = await loadLaunchPlatform(upstreamRoot);
    const manifest = JSON.parse(readFileSync(new URL('../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url), 'utf8'));
    assert.equal(platform.parseArgs([]).version, manifest.version);
  } finally { hook.deregister(); }
});

test('custom profile template validation precedes encrypted bundle or profile writes', async () => {
  const { prepareProfile } = await import('../src/platform/profile.mjs');
  let initialized;
  const platform = { boot: { resolveProfileDir: () => '/isolated/data/profiles/custom' },
    async initializeProfileFromDefault(...args) { initialized = args; throw Error('existing profile rejected'); } };
  await assert.rejects(prepareProfile({ dataRoot: '/isolated/data', upstreamRoot, mode: 'headless',
    invocation: { profile: 'custom', fromDefaultProfile: 'headless', args: [], patches: [] } }, { platform, plugins: {} }), /existing profile rejected/);
  assert.deepEqual(initialized, ['custom', 'headless', '/isolated/data']);
});

test('custom profile initialization refuses an existing target without replacing its user files', async () => {
  const { mkdtemp, mkdir, writeFile, readFile } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { prepareProfile } = await import('../src/platform/profile.mjs');
  const root = await mkdtemp(join(tmpdir(), 'dsh-profile-preserve-'));
  const target = join(root, 'profiles/custom');
  await mkdir(target, { recursive: true });
  const marker = join(target, 'cordis.patch.yml');
  await writeFile(marker, 'user-profile-must-remain\n');
  let initialized = false;
  const platform = { boot: { resolveProfileDir: () => target },
    async initializeProfileFromDefault() { initialized = true; throw Error('unsafe replacement invoked'); } };
  await assert.rejects(prepareProfile({ dataRoot: root, mode: 'headless',
    invocation: { profile: 'custom', fromDefaultProfile: 'headless', args: [], patches: [] } },
  { platform, plugins: {} }), /already exists.*preserve/i);
  assert.equal(initialized, false);
  assert.equal(await readFile(marker, 'utf8'), 'user-profile-must-remain\n');
});

test('fresh profile initializer preserves existing directories and initializes only a new validated target', async () => {
  const { mkdtemp, mkdir, readFile, writeFile } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const api = await import('../src/platform/launch-platform.mjs');
  assert.equal(typeof api.initializeFreshProfile, 'function');
  const root = await mkdtemp(join(tmpdir(), 'dsh-fresh-profile-'));
  const templates = { headless: { bundles: ['@deepseek-ai/dsh-headless'] } };
  const boot = { resolveProfileDir: (name, home) => join(home, 'profiles', name),
    async initProfile(dir, bundles) { await writeFile(join(dir, 'package.json'), JSON.stringify({ bundles })); } };
  await api.initializeFreshProfile(boot, templates, 'custom', 'headless', root);
  assert.deepEqual(JSON.parse(await readFile(join(root, 'profiles/custom/package.json'), 'utf8')),
    { bundles: ['@deepseek-ai/dsh-headless'] });
  await assert.rejects(api.initializeFreshProfile(boot, templates, 'custom', 'headless', root), /already exists.*preserve/i);
  await assert.rejects(api.initializeFreshProfile(boot, templates, 'Headless', 'headless', root), /shipped profile/i);
  await assert.rejects(api.initializeFreshProfile(boot, templates, 'other', 'missing', root), /unknown default profile/i);
  const existing = join(root, 'profiles/existing');
  await mkdir(existing);
  await writeFile(join(existing, 'marker'), 'keep');
  await assert.rejects(api.initializeFreshProfile(boot, templates, 'existing', 'headless', root), /already exists.*preserve/i);
  assert.equal(await readFile(join(existing, 'marker'), 'utf8'), 'keep');
});

test('built command loading follows the installed CLI dispatcher without executing either bin entry', async () => {
  const { mkdtemp, mkdir, writeFile } = await import('node:fs/promises');
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const api = await import('../src/platform/launch-platform.mjs');
  assert.equal(typeof api.loadBuiltCommand, 'function');
  const root = await mkdtemp(join(tmpdir(), 'dsh-cli-dispatch-'));
  const lib = join(root, 'apps/cli/lib');
  await mkdir(lib, { recursive: true });
  await writeFile(join(root, 'package.json'), '{"type":"module"}');
  await writeFile(join(lib, 'bin.js'), 'throw Error("Do not execute installed launcher"); import("./bin-entry.js");');
  await writeFile(join(lib, 'bin-entry.js'), 'throw Error("Do not execute dispatcher"); import("./plugin-command.js");');
  await writeFile(join(lib, 'plugin-command.js'), 'export async function runPlugin(profile) { return `loaded:${profile}`; }');
  const command = await api.loadBuiltCommand(root, 'plugin');
  assert.equal(await command.runPlugin('web'), 'loaded:web');
  await assert.rejects(api.loadBuiltCommand(root, 'unsupported'), /Unsupported native CLI/);
  await writeFile(join(lib, 'bin-entry.js'), 'import("./dump-config-fixture.js"); import("./dump-config-schema-fixture.js");');
  await writeFile(join(lib, 'dump-config-fixture.js'), 'export const kind = "config";');
  assert.equal((await api.loadBuiltCommand(root, 'dump-config')).kind, 'config');
  await writeFile(join(lib, 'bin-entry.js'), 'import("./plugin-command.js"); import("./plugin-second.js");');
  await assert.rejects(api.loadBuiltCommand(root, 'plugin'), /build entry is unavailable/);
});

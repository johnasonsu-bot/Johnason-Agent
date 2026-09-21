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

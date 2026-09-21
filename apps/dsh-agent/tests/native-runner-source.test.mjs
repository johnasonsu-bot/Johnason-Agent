import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const upstreamRoot = fileURLToPath(new URL('../../../third_party/deepseek-harness', import.meta.url));
const runnerUrl = new URL('../src/native-runner.mjs', import.meta.url).href;
const profileUrl = new URL('../src/profile.mjs', import.meta.url).href;

// Execute the actual launcher source. Only the unavailable native-package
// boundaries are replaced; no packages, profiles, or service processes start.
const harness = String.raw`
  import { readFile } from 'node:fs/promises';
  import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';
  const options = JSON.parse(process.argv[1]);
  const observed = {};
  const config = {
    upstreamRoot: options.upstreamRoot, dataRoot: '/isolated/dsh-home',
    mode: 'plugin', args: ['plugin', '--profile', 'custom', 'list', '--depth', '0'],
  };
  const invocation = { mode: 'plugin', profile: 'custom', args: ['list', '--depth', '0'] };
  const fakeProcess = { argv: ['node', 'native-runner.mjs', JSON.stringify(config)], env: {} };
  const context = createContext({ process: fakeProcess, URL });
  const mocks = new Map();
  if (options.kind === 'runner') {
    mocks.set('./profile.mjs', { prepareProfile: async launch => ({
      profile: launch.invocation.profile, args: launch.invocation.args, patchFiles: [],
    }) });
    mocks.set('./systems.mjs', { readProviders: () => [] });
    mocks.set('./dataplatform.mjs', { prepareTrustedCore() {}, CORE_ENV_KEYS: [] });
    mocks.set(new URL('apps/cli/src/args.ts', 'file://' + options.upstreamRoot + '/').href, {
      parseDshArgs: (args, version) => { observed.version = version; return invocation; },
    });
    mocks.set(new URL('apps/cli/src/plugin.ts', 'file://' + options.upstreamRoot + '/').href, {
      runPlugin: async (profile, args) => {
        observed.profile = profile; observed.args = args;
        await new Promise(resolve => setImmediate(resolve));
        observed.pluginSettled = true;
        return options.exitCode;
      },
    });
  } else {
    mocks.set('node:module', { createRequire: () => ({ resolve: () => 'fixture:boot' }) });
    mocks.set('fixture:boot', {
      resolveProfileDir: () => '/isolated/dsh-home/profiles/custom',
      resolveBundleDir: () => { throw new Error('bundle composition reached before template validation'); },
    });
    mocks.set('./systems.mjs', { readProviders: () => [] });
    mocks.set(new URL('apps/cli/src/profile-boot.ts', 'file://' + options.upstreamRoot + '/').href, {
      initializeProfileFromDefault: (profile, template, home) => {
        observed.initialization = { profile, template, home };
        throw new Error('existing profile rejected by native initializer');
      },
    });
  }
  const cache = new Map();
  async function dependency(specifier) {
    specifier = String(specifier);
    if (!cache.has(specifier)) {
      const exports = mocks.get(specifier) ?? (specifier.startsWith('node:') ? await import(specifier) : undefined);
      if (!exports) throw new Error('Unexpected dependency: ' + specifier);
      const names = Object.keys(exports);
      cache.set(specifier, new SyntheticModule(names, function() {
        for (const name of names) this.setExport(name, exports[name]);
      }, { context, identifier: specifier }));
    }
    return cache.get(specifier);
  }
  const url = options.kind === 'runner' ? options.runnerUrl : options.profileUrl;
  const source = new SourceTextModule(await readFile(new URL(url), 'utf8'), {
    context, identifier: url,
    initializeImportMeta: meta => { meta.url = url; },
    importModuleDynamically: async specifier => {
      const module = await dependency(specifier);
      if (module.status === 'unlinked') await module.link(dependency);
      if (module.status === 'linked') await module.evaluate();
      return module;
    },
  });
  try {
    await source.link(dependency);
    await source.evaluate();
    if (options.kind === 'profile') await source.namespace.prepareProfile({
      ...config, mode: 'headless',
      invocation: { mode: 'profile', profile: 'custom', fromDefaultProfile: 'headless', args: ['hello'], patches: [] },
    });
    observed.exitCode = fakeProcess.exitCode;
  } catch (error) {
    observed.error = error.message;
  }
  process.stdout.write(JSON.stringify(observed));
`;

function executeSource(options) {
  const child = spawnSync(process.execPath, ['--experimental-vm-modules', '--input-type=module', '-e', harness,
    JSON.stringify({ upstreamRoot, runnerUrl, profileUrl, ...options })], {
    encoding: 'utf8', timeout: 10000,
    env: { PATH: process.env.PATH },
  });
  assert.equal(child.status, 0, child.stderr);
  return JSON.parse(child.stdout);
}

test('native plugin commands await completion and propagate the numeric exit status', () => {
  for (const exitCode of [0, 17]) {
    const observed = executeSource({ kind: 'runner', exitCode });
    assert.equal(observed.error, undefined);
    assert.equal(observed.pluginSettled, true);
    assert.equal(observed.exitCode, exitCode);
    assert.equal(observed.profile, 'custom');
    assert.deepEqual(observed.args, ['list', '--depth', '0']);
  }
});

test('native version reporting uses the selected upstream CLI manifest', () => {
  const manifest = JSON.parse(readFileSync(new URL('../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url), 'utf8'));
  const observed = executeSource({ kind: 'runner', exitCode: 0 });
  assert.equal(observed.version, manifest.version);
});

test('custom profile template validation runs before encrypted bundle or profile writes', () => {
  const observed = executeSource({ kind: 'profile' });
  assert.equal(observed.error, 'existing profile rejected by native initializer');
  assert.deepEqual(observed.initialization, {
    profile: 'custom', template: 'headless', home: '/isolated/dsh-home',
  });
});

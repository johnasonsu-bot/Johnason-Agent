import { readFile, mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { nativeRequire, nativeUrl } from './runtime.mjs';

/** Resolve the pinned CLI's lazy built entry without running its dotenv/bin path. */
export async function loadBuiltCommand(upstreamRoot, command) {
  if (!['plugin', 'dump-config', 'dump-config-schema'].includes(command)) throw new Error('Unsupported native CLI command');
  let bin = await readFile(nativeUrl('apps/cli/lib/bin.js', upstreamRoot), 'utf8');
  const dispatchers = [...bin.matchAll(/import\(["']\.\/(bin-[A-Za-z0-9_-]+\.js)["']\)/g)];
  if (dispatchers.length > 1) throw new Error('Pinned native CLI dispatcher build entry is unavailable');
  if (dispatchers.length === 1) bin = await readFile(nativeUrl(`apps/cli/lib/${dispatchers[0][1]}`, upstreamRoot), 'utf8');
  const matches = [...bin.matchAll(new RegExp(`import\\(["']\\./(${command}-[A-Za-z0-9_-]+\\.js)["']\\)`, 'g'))];
  const entries = [...new Set(matches.map(match => match[1]))]
    .filter(entry => command !== 'dump-config' || !entry.startsWith('dump-config-schema-'));
  if (entries.length !== 1) throw new Error(`Pinned native CLI ${command} build entry is unavailable`);
  return import(nativeUrl(`apps/cli/lib/${entries[0]}`, upstreamRoot));
}

/** Version-specific launcher/profile calls live here, never in business plugins. */
export async function loadLaunchPlatform(upstreamRoot) {
  const require = nativeRequire(upstreamRoot);
  const manifest = JSON.parse(await readFile(nativeUrl('apps/cli/package.json', upstreamRoot), 'utf8'));
  const { parseDshArgs } = await import(nativeUrl('apps/cli/src/args.ts', upstreamRoot));
  if (typeof manifest.version !== 'string') throw new Error('Native CLI version is missing');
  return {
    version: manifest.version,
    parseArgs: args => parseDshArgs(args, manifest.version),
    async runPlugin(profile, args) {
      return (await loadBuiltCommand(upstreamRoot, 'plugin')).runPlugin(profile, args);
    },
    async runDumpConfig(profile, defaultOnly, patches) {
      return (await loadBuiltCommand(upstreamRoot, 'dump-config')).runDumpConfig(profile, defaultOnly, patches);
    },
    async runProfile(options) {
      return (await import(nativeUrl('apps/cli/lib/profile-boot.js', upstreamRoot))).runProfile(options);
    },
    async environment(values) {
      const { createLaunchEnvironmentSnapshot } = await import(require.resolve('@deepseek-ai/dsh-launch-environment'));
      return createLaunchEnvironmentSnapshot([{ source: 'process', values }]);
    },
  };
}

export async function loadProfilePlatform(upstreamRoot) {
  const require = nativeRequire(upstreamRoot);
  const boot = await import(require.resolve('@deepseek-ai/dsh-app-boot'));
  const { PROFILE_TEMPLATES } = await import(require.resolve('@deepseek-ai/dsh-official-bundles'));
  return {
    boot,
    anchor: fileURLToPath(nativeUrl('apps/cli/package.json', upstreamRoot)),
    authorizationEntry: fileURLToPath(nativeUrl('packages/credentials/authorization/lib/index.js', upstreamRoot)),
    async initializeProfileFromDefault(...args) {
      return initializeFreshProfile(boot, PROFILE_TEMPLATES, ...args);
    },
  };
}

/** Initialize a new custom profile without the native replace-and-delete behavior. */
export async function initializeFreshProfile(boot, templates, name, fromDefault, home) {
  const target = boot.resolveProfileDir(name, home);
  if (!Object.hasOwn(templates, fromDefault)) throw new Error(`Unknown default profile ${JSON.stringify(fromDefault)}`);
  if (Object.keys(templates).some(shipped => shipped.toLowerCase() === name.toLowerCase())) {
    throw new Error(`Profile ${JSON.stringify(name)} is a shipped profile and cannot be a custom target`);
  }
  await mkdir(dirname(target), { recursive: true, mode: 0o700 });
  try { await mkdir(target, { mode: 0o700 }); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error(`Profile ${JSON.stringify(name)} already exists; preserve its files and select a new profile name`);
    throw error;
  }
  // Keep a partial directory on failure so diagnostics never destroy user files.
  await boot.initProfile(target, templates[fromDefault].bundles);
}

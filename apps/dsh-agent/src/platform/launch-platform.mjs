import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { nativeRequire, nativeUrl } from './runtime.mjs';

/** Resolve the pinned CLI's lazy built entry without running its dotenv/bin path. */
async function builtCommand(upstreamRoot, command) {
  if (!['plugin', 'dump-config'].includes(command)) throw new Error('Unsupported native CLI command');
  const bin = await readFile(nativeUrl('apps/cli/lib/bin.js', upstreamRoot), 'utf8');
  const matches = [...bin.matchAll(new RegExp(`import\\(["']\\./(${command}-[A-Za-z0-9_-]+\\.js)["']\\)`, 'g'))];
  const entries = [...new Set(matches.map(match => match[1]))];
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
      return (await builtCommand(upstreamRoot, 'plugin')).runPlugin(profile, args);
    },
    async runDumpConfig(profile, defaultOnly, patches) {
      return (await builtCommand(upstreamRoot, 'dump-config')).runDumpConfig(profile, defaultOnly, patches);
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
  return {
    boot: await import(require.resolve('@deepseek-ai/dsh-app-boot')),
    anchor: fileURLToPath(nativeUrl('apps/cli/package.json', upstreamRoot)),
    authorizationEntry: fileURLToPath(nativeUrl('packages/credentials/authorization/lib/index.js', upstreamRoot)),
    async initializeProfileFromDefault(...args) {
      return (await import(nativeUrl('apps/cli/lib/profile-boot.js', upstreamRoot))).initializeProfileFromDefault(...args);
    },
  };
}

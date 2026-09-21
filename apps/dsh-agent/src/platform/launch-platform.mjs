import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { nativeRequire, nativeUrl } from './runtime.mjs';

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
      return (await import(nativeUrl('apps/cli/src/plugin.ts', upstreamRoot))).runPlugin(profile, args);
    },
    async runDumpConfig(profile, defaultOnly, patches) {
      return (await import(nativeUrl('apps/cli/src/dump-config.ts', upstreamRoot))).runDumpConfig(profile, defaultOnly, patches);
    },
    async runProfile(options) {
      return (await import(nativeUrl('apps/cli/src/profile-boot.ts', upstreamRoot))).runProfile(options);
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
      return (await import(nativeUrl('apps/cli/src/profile-boot.ts', upstreamRoot))).initializeProfileFromDefault(...args);
    },
  };
}

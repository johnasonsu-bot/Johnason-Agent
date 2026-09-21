import { createRequire } from 'node:module';
import { existsSync, lstatSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repositoryRoot = fileURLToPath(new URL('../../../../', import.meta.url));
export const DEFAULT_UPSTREAM_ROOT = join(repositoryRoot, 'third_party/deepseek-harness');
export const PLATFORM_RELEASE_PATH = join(repositoryRoot, 'mvp/src/workbench/runtime/deepseek_harness/platform-release.json');

/** Shared JS/Python release policy; never accepts floating revisions or ABI drift. */
export function readPlatformRelease(path = PLATFORM_RELEASE_PATH) {
  if (!lstatSync(path).isFile()) throw new Error('DSH platform release must be a regular file');
  const value = JSON.parse(readFileSync(path, 'utf8'));
  if (!value || Array.isArray(value) || value.schema !== 'johnason.dsh.platform.v1'
      || Object.keys(value).sort().join(',') !== 'adapterContract,packageManager,revision,schema,version'
      || !/^[a-f0-9]{40}$/.test(value.revision ?? '') || value.adapterContract !== 1
      || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/.test(value.version ?? '')
      || !/^pnpm@\d+\.\d+\.\d+$/.test(value.packageManager ?? '')) {
    throw new Error('Invalid or unsupported DSH platform release contract');
  }
  return Object.freeze(value);
}

export function nativeRequire(upstreamRoot = DEFAULT_UPSTREAM_ROOT) {
  return createRequire(join(upstreamRoot, 'apps/cli/package.json'));
}

export function nativeUrl(relative, upstreamRoot = DEFAULT_UPSTREAM_ROOT) {
  return pathToFileURL(join(upstreamRoot, relative));
}

export function platformPaths(repoRoot = repositoryRoot) {
  const upstreamRoot = join(repoRoot, 'third_party/deepseek-harness');
  return Object.freeze({ upstreamRoot,
    anchor: join(upstreamRoot, 'apps/cli/package.json'),
    cliArtifact: join(upstreamRoot, 'apps/cli/lib/bin.js'),
    webArtifact: join(upstreamRoot, 'apps/web/dist/index.html'),
    tsconfig: join(upstreamRoot, 'tsconfig.json'),
    authorizationEntry: join(upstreamRoot, 'packages/credentials/authorization/lib/index.js'),
  });
}

export function inspectPlatformBuild(upstreamRoot = DEFAULT_UPSTREAM_ROOT) {
  const required = [['CLI build artifact', join(upstreamRoot, 'apps/cli/lib/bin.js')],
    ['Web build artifact', join(upstreamRoot, 'apps/web/dist/index.html')]];
  const diagnostics = required.filter(([, path]) => !existsSync(path)).map(([label, path]) => `${label} missing: ${path}`);
  return { built: diagnostics.length === 0, diagnostics };
}

export function nativeLoaderOptions(upstreamRoot = DEFAULT_UPSTREAM_ROOT) {
  const require = createRequire(join(upstreamRoot, 'package.json'));
  return { hook: require.resolve('tsx/esm'), tsconfig: join(upstreamRoot, 'tsconfig.json') };
}

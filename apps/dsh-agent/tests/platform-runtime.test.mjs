import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

const runtimeUrl = new URL('../src/platform/runtime.mjs', import.meta.url);

test('native loader preserves the tool scheduler identity across package and plugin imports', async () => {
  const api = await import(runtimeUrl);
  const { hook, tsconfig } = api.nativeLoaderOptions();
  const built = api.nativeRequire().resolve('@deepseek-ai/dsh-tools');
  const script = `const a = await import('@deepseek-ai/dsh-tools'); const b = await import(${JSON.stringify(built)}); if (a.TOOL_RUNTIME_SCHEDULER !== b.TOOL_RUNTIME_SCHEDULER) throw new Error('Tool scheduler identity split');`;
  const child = spawnSync(process.execPath, ['--import', hook, '--input-type=module', '-e', script], {
    cwd: join(api.DEFAULT_UPSTREAM_ROOT, 'apps/cli'), env: { PATH: process.env.PATH, TSX_TSCONFIG_PATH: tsconfig }, encoding: 'utf8', timeout: 15000,
  });
  assert.equal(child.status, 0, child.stderr);
});

test('platform owns upstream layout and resolves paths without loading plugins', async () => {
  const api = await import(runtimeUrl).catch(() => ({}));
  assert.equal(typeof api.platformPaths, 'function');
  const paths = api.platformPaths('/tmp/repository with spaces');
  assert.equal(paths.upstreamRoot, '/tmp/repository with spaces/third_party/deepseek-harness');
  assert.equal(paths.cliArtifact, join(paths.upstreamRoot, 'apps/cli/lib/bin.js'));
  assert.equal(paths.webArtifact, join(paths.upstreamRoot, 'apps/web/dist/index.html'));
  assert.equal(api.nativeUrl('apps/cli/package.json', paths.upstreamRoot).pathname,
    '/tmp/repository%20with%20spaces/third_party/deepseek-harness/apps/cli/package.json');
});

test('platform release refuses malformed or unsupported contracts before a build', async () => {
  const api = await import(runtimeUrl).catch(() => ({}));
  assert.equal(typeof api.readPlatformRelease, 'function');
  const root = mkdtempSync(join(tmpdir(), 'dsh-platform-release-'));
  const path = join(root, 'release.json');
  const release = { schema: 'johnason.dsh.platform.v1', revision: 'd'.repeat(40), version: '0.1.6-alpha.2',
    packageManager: 'pnpm@11.7.0', adapterContract: 1 };
  writeFileSync(path, JSON.stringify(release));
  assert.deepEqual(api.readPlatformRelease(path), release);
  for (const bad of [{ ...release, revision: 'master' }, { ...release, adapterContract: 2 },
    { ...release, packageManager: 'pnpm@latest' }, { ...release, version: null },
    { ...release, undocumented: true }, { ...release, version: '1.0.0-_invalid' }]) {
    writeFileSync(path, JSON.stringify(bad));
    assert.throws(() => api.readPlatformRelease(path), /platform release/i);
  }
});

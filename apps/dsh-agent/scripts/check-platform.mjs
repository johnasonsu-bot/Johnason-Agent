#!/usr/bin/env node
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { readPlatformRelease, platformPaths } from '../src/platform/runtime.mjs';

export function assertReleaseAgreement(release, actual) {
  for (const [key, expected] of Object.entries({ head: release.revision, gitlink: release.revision,
    version: release.version, packageManager: release.packageManager })) {
    if (actual[key] !== expected) throw new Error(`DSH platform contract mismatch: ${key}`);
  }
}

/** Business code may consume the platform SDK, never locate official implementations. */
export function auditBoundaries(repoRoot) {
  const violations = [];
  const upstreamImport = /third_party[\/]deepseek-harness|apps[\/]cli[\/](?:src|lib)|(?:from\s*|import\s*\(|\.resolve\s*\()\s*['"]@deepseek-ai\//;
  function scan(path) {
    if (!existsSync(path)) return;
    for (const item of readdirSync(path, { withFileTypes: true })) {
      const target = join(path, item.name);
      const local = relative(repoRoot, target).replaceAll('\\', '/');
      if (local.startsWith('apps/dsh-agent/src/platform/') || local === 'mvp/sidecars/deepseek-harness/src/platform-runtime.ts') continue;
      if (item.isDirectory()) scan(target);
      else if (/\.(mjs|ts)$/.test(item.name) && upstreamImport.test(readFileSync(target, 'utf8'))) violations.push(local);
    }
  }
  scan(join(repoRoot, 'apps/dsh-agent/src'));
  scan(join(repoRoot, 'mvp/sidecars/deepseek-harness/src'));
  return violations.sort();
}

export function checkPlatform(repoRoot) {
  const release = readPlatformRelease(join(repoRoot, 'mvp/src/workbench/runtime/deepseek_harness/platform-release.json'));
  const { upstreamRoot } = platformPaths(repoRoot);
  const git = (cwd, args) => {
    const result = spawnSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    if (result.status !== 0) throw new Error('Unable to verify DSH platform source');
    return result.stdout.trim();
  };
  const packageJson = JSON.parse(readFileSync(join(upstreamRoot, 'package.json'), 'utf8'));
  const cliJson = JSON.parse(readFileSync(join(upstreamRoot, 'apps/cli/package.json'), 'utf8'));
  const gitlink = git(repoRoot, ['ls-tree', 'HEAD', 'third_party/deepseek-harness']).match(/^160000 commit ([a-f0-9]{40})\s/)?.[1];
  assertReleaseAgreement(release, { head: git(upstreamRoot, ['rev-parse', 'HEAD']), gitlink,
    version: cliJson.version, packageManager: packageJson.packageManager });
  if (git(upstreamRoot, ['status', '--porcelain'])) throw new Error('DSH platform contract requires a clean official checkout');
  const violations = auditBoundaries(repoRoot);
  if (violations.length) throw new Error('Native dependencies outside platform adapters: ' + violations.join(', '));
  return { revision: release.revision, version: release.version, adapterContract: release.adapterContract, boundaries: 'passed' };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(checkPlatform(fileURLToPath(new URL('../../../', import.meta.url))), null, 2)); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}

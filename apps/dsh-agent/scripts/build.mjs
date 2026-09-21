#!/usr/bin/env node
import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readPlatformRelease, platformPaths } from '../src/platform/runtime.mjs'

const release = readPlatformRelease()
export const PINNED_DSH_SHA = release.revision

export function runNativeBuild({ repoRoot, spawn = spawnSync, env = process.env }) {
  const { upstreamRoot } = platformPaths(repoRoot)
  const revision = spawn('git', ['rev-parse', 'HEAD'], {
    cwd: upstreamRoot,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  if (revision.status !== 0) {
    process.stderr.write('Unable to read the DeepSeek Harness submodule revision.\n')
    return revision.status ?? 1
  }
  if (revision.stdout.trim() !== PINNED_DSH_SHA) {
    process.stderr.write(`DeepSeek Harness checkout is not pinned to ${PINNED_DSH_SHA}.\n`)
    return 1
  }

  // Upstream's postinstall explicitly honors CI=true by skipping only its
  // Git-hook mutation. This is needed for a submodule inside a linked worktree;
  // dependency lifecycle scripts and all build checks still run normally.
  const childEnv = { ...env, CI: 'true' }
  const commands = [
    ['corepack', [release.packageManager, 'install', '--frozen-lockfile']],
    ['corepack', [release.packageManager, 'run', 'build']],
  ]
  for (const [command, args] of commands) {
    const result = spawn(command, args, {
      cwd: upstreamRoot,
      env: childEnv,
      stdio: 'inherit',
    })
    if (result.status !== 0) return result.status ?? 1
  }
  return 0
}

const entryPath = process.argv[1] ? resolve(process.argv[1]) : ''
if (entryPath === fileURLToPath(import.meta.url)) {
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
  process.exitCode = runNativeBuild({ repoRoot })
}

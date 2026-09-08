#!/usr/bin/env node
import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const PINNED_DSH_SHA = 'b150a551b8d465e31e418e1b2eaf5e79bbb7d28e'

export function runNativeBuild({ repoRoot, spawn = spawnSync, env = process.env }) {
  const upstreamRoot = join(repoRoot, 'third_party/deepseek-harness')
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
    ['corepack', ['pnpm@11.7.0', 'install', '--frozen-lockfile']],
    ['corepack', ['pnpm@11.7.0', 'run', 'build']],
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

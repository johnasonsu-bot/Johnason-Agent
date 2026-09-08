import assert from 'node:assert/strict'
import test from 'node:test'

const buildUrl = new URL('../scripts/build.mjs', import.meta.url)

test('native build verifies the pinned checkout then installs and builds in order', async () => {
  const mod = await import(buildUrl).catch(() => ({}))
  assert.equal(typeof mod.runNativeBuild, 'function')

  const calls = []
  const spawn = (command, args, options) => {
    calls.push({ command, args, options })
    if (command === 'git') {
      return { status: 0, stdout: `${mod.PINNED_DSH_SHA}\n`, stderr: '' }
    }
    return { status: 0, stdout: '', stderr: '' }
  }
  const status = mod.runNativeBuild({ repoRoot: '/tmp/repo root', spawn, env: { PATH: '/usr/bin' } })

  assert.equal(status, 0)
  assert.deepEqual(calls.map(({ command, args }) => [command, args]), [
    ['git', ['rev-parse', 'HEAD']],
    ['corepack', ['pnpm@11.7.0', 'install', '--frozen-lockfile']],
    ['corepack', ['pnpm@11.7.0', 'run', 'build']],
  ])
  assert.equal(calls[1].options.cwd, '/tmp/repo root/third_party/deepseek-harness')
  assert.equal(calls[1].options.env.CI, 'true')
})

test('native build refuses an unpinned checkout before invoking corepack', async () => {
  const { runNativeBuild } = await import(buildUrl)
  const calls = []
  const spawn = (command, args) => {
    calls.push([command, args])
    return { status: 0, stdout: '0000000000000000000000000000000000000000\n', stderr: '' }
  }

  assert.equal(runNativeBuild({ repoRoot: '/tmp/repo', spawn, env: {} }), 1)
  assert.equal(calls.length, 1)
})

test('native build preserves the failing command exit status and stops', async () => {
  const mod = await import(buildUrl)
  let calls = 0
  const spawn = command => {
    calls += 1
    if (command === 'git') return { status: 0, stdout: `${mod.PINNED_DSH_SHA}\n`, stderr: '' }
    return { status: 37, stdout: '', stderr: '' }
  }

  assert.equal(mod.runNativeBuild({ repoRoot: '/tmp/repo', spawn, env: {} }), 37)
  assert.equal(calls, 2)
})

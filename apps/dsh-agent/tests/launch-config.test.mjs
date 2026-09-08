import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import test from 'node:test'

const moduleUrl = new URL('../src/launch-config.mjs', import.meta.url)

async function loadLauncher() {
  return import(moduleUrl).catch(() => ({}))
}

const baseOptions = {
  homeDir: '/tmp/user home',
  repoRoot: '/tmp/repo root',
  env: {
    PATH: '/usr/bin',
    HOME: '/tmp/user home',
    LANG: 'zh_CN.UTF-8',
    DSH_HOME: '/tmp/old-dsh',
    DEEPSEEK_API_KEY: 'synthetic-secret-do-not-use',
    AWS_SECRET_ACCESS_KEY: 'synthetic-secret-do-not-use',
  },
  nodeVersion: '22.20.0',
}

test('exports the launch resolver and defaults to an isolated data root', async () => {
  const mod = await loadLauncher()
  assert.equal(typeof mod.resolveLaunch, 'function')

  const result = mod.resolveLaunch(['web'], baseOptions)
  assert.equal(result.mode, 'web')
  assert.equal(result.dataRoot, '/tmp/user home/.johnason-dsh')
  assert.equal(result.upstreamRoot, '/tmp/repo root/third_party/deepseek-harness')
})

test('preserves paths with spaces and translates owned web options', async () => {
  const { resolveLaunch } = await loadLauncher()
  const result = resolveLaunch([
    'web', '--data-dir', '/tmp/data root', '--workspace', '/tmp/work space',
    '--port', '4310', '--no-open',
  ], baseOptions)

  assert.equal(result.dataRoot, '/tmp/data root')
  assert.equal(result.workspace, '/tmp/work space')
  assert.deepEqual(result.args, ['web', '--port', '4310', '--no-open'])
})

test('does not inherit old DSH_HOME or credential-like environment variables', async () => {
  const { resolveLaunch } = await loadLauncher()
  const result = resolveLaunch(['web'], baseOptions)

  assert.deepEqual(result.env, {
    PATH: '/usr/bin',
    HOME: '/tmp/user home',
    LANG: 'zh_CN.UTF-8',
    DSH_HOME: '/tmp/user home/.johnason-dsh',
    DSH_TELEMETRY_DISABLED: '1',
  })
  assert.equal('DEEPSEEK_API_KEY' in result.env, false)
  assert.equal('AWS_SECRET_ACCESS_KEY' in result.env, false)
})

test('headless requires an explicit workspace and forwards the task verbatim', async () => {
  const { resolveLaunch } = await loadLauncher()
  assert.throws(
    () => resolveLaunch(['headless', 'hello'], baseOptions),
    /workspace/i,
  )

  const result = resolveLaunch(
    ['headless', '--workspace', '/tmp/work space', 'write', 'a file'],
    baseOptions,
  )
  assert.deepEqual(result.args, ['--profile', 'headless', 'write', 'a file'])
})

test('plugin requires a workspace and forwards only plugin arguments', async () => {
  const { resolveLaunch } = await loadLauncher()
  const result = resolveLaunch(
    ['plugin', '--workspace', '/tmp/plugin work', '--profile', 'tui', 'add', '@example/tool'],
    baseOptions,
  )
  assert.equal(result.mode, 'plugin')
  assert.deepEqual(result.args, ['plugin', '--profile', 'tui', 'add', '@example/tool'])
})

test('rejects unsupported Node versions, malformed options, and unknown modes', async () => {
  const { resolveLaunch } = await loadLauncher()
  for (const nodeVersion of ['20.19.0', '22.18.9', '23.0.0']) {
    assert.throws(() => resolveLaunch(['web'], { ...baseOptions, nodeVersion }), /Node.*22\.19.*24/i)
  }
  for (const nodeVersion of ['22.19.0', '22.99.1', '24.0.0', '25.1.0']) {
    assert.doesNotThrow(() => resolveLaunch(['web'], { ...baseOptions, nodeVersion }))
  }
  assert.throws(() => resolveLaunch(['web', '--port', '0'], baseOptions), /port/i)
  assert.throws(() => resolveLaunch(['web', '--port', '65536'], baseOptions), /port/i)
  assert.throws(() => resolveLaunch(['web', '--port'], baseOptions), /--port.*value/i)
  assert.throws(() => resolveLaunch(['web', '--workspace'], baseOptions), /--workspace.*value/i)
  assert.throws(() => resolveLaunch(['web', '--wat'], baseOptions), /unknown option/i)
  assert.throws(() => resolveLaunch(['serve'], baseOptions), /unknown mode/i)
})

test('doctor reports absent and present native build artifacts', async () => {
  const { resolveLaunch } = await loadLauncher()
  const repoRoot = mkdtempSync(join(tmpdir(), 'dsh doctor repo '))
  const missing = resolveLaunch(['doctor'], { ...baseOptions, repoRoot })
  assert.equal(missing.mode, 'doctor')
  assert.equal(missing.built, false)
  assert.match(missing.diagnostics.join('\n'), /CLI build artifact.*missing/i)
  assert.match(missing.diagnostics.join('\n'), /Web build artifact.*missing/i)

  mkdirSync(join(repoRoot, 'third_party/deepseek-harness/apps/cli/lib'), { recursive: true })
  mkdirSync(join(repoRoot, 'third_party/deepseek-harness/apps/web/dist'), { recursive: true })
  writeFileSync(join(repoRoot, 'third_party/deepseek-harness/apps/cli/lib/bin.js'), '')
  writeFileSync(join(repoRoot, 'third_party/deepseek-harness/apps/web/dist/index.html'), '')
  const present = resolveLaunch(['doctor'], { ...baseOptions, repoRoot })
  assert.equal(present.built, true)
  assert.deepEqual(present.diagnostics, [])
})

test('CLI blocks execution before an encrypted profile is available', () => {
  const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url))
  const result = spawnSync(process.execPath, [cli, 'web'], {
    encoding: 'utf8',
    env: { PATH: process.env.PATH, HOME: '/tmp/user' },
  })
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /PROFILE_NOT_READY/)
  assert.doesNotMatch(result.stderr, /synthetic-secret-do-not-use/)
})

test('CLI exposes the profile preparation boundary with a stable error code', async () => {
  const { prepareProfile } = await import(new URL('../src/cli.mjs', import.meta.url))
  assert.equal(typeof prepareProfile, 'function')
  assert.throws(
    () => prepareProfile({ mode: 'web' }),
    error => error?.code === 'PROFILE_NOT_READY',
  )
})

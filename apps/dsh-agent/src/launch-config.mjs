import { existsSync } from 'node:fs'
import { join, resolve, isAbsolute } from 'node:path'

const SAFE_ENV_KEYS = new Set([
  'PATH', 'HOME', 'USERPROFILE', 'SystemRoot', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL',
])

function assertSupportedNode(nodeVersion) {
  const match = /^(\d+)\.(\d+)\.(\d+)(?:-|$)/.exec(nodeVersion)
  if (!match) throw new Error(`Invalid Node version: ${nodeVersion}`)
  const major = Number(match[1])
  const minor = Number(match[2])
  if (!((major === 22 && minor >= 19) || major >= 24)) {
    throw new Error(`Node ${nodeVersion} is unsupported; use Node 22.19+ in the 22.x line, or Node 24+`)
  }
}
function takeValue(argv, index, option) {
  const value = argv[index + 1]
  if (value === undefined || value.startsWith('--')) {
    throw new Error(`${option} requires a value`)
  }
  return value
}

function inspectBuild(upstreamRoot) {
  const required = [
    ['CLI build artifact', join(upstreamRoot, 'apps/cli/lib/bin.js')],
    ['Web build artifact', join(upstreamRoot, 'apps/web/dist/index.html')],
  ]
  const diagnostics = required
    .filter(([, artifact]) => !existsSync(artifact))
    .map(([label, artifact]) => `${label} missing: ${artifact}`)
  return { built: diagnostics.length === 0, diagnostics }
}

export function resolveLaunch(argv, { homeDir, repoRoot, env, nodeVersion }) {
  assertSupportedNode(nodeVersion)
  const mode = argv[0]
  if (!['web', 'headless', 'plugin', 'doctor'].includes(mode)) {
    throw new Error(`Unknown mode: ${mode ?? '(missing)'}`)
  }

  let dataRoot = join(homeDir, '.johnason-dsh')
  let workspace
  let corePath
  let port
  let noOpen = false
  const passthrough = []

  for (let index = 1; index < argv.length; index += 1) {
    const token = argv[index]
    if (token === '--') {
      passthrough.push(...argv.slice(index))
      break
    } else if (token === '--data-dir') {
      dataRoot = resolve(takeValue(argv, index, token))
      index += 1
    } else if (token === '--core') {
      corePath = takeValue(argv, index, token)
      if (!isAbsolute(corePath)) throw new Error('--core requires an absolute trusted path')
      index += 1
    } else if (token === '--workspace') {
      workspace = resolve(takeValue(argv, index, token))
      index += 1
    } else if (token === '--port') {
      if (mode !== 'web') throw new Error('--port is only valid in web mode')
      const raw = takeValue(argv, index, token)
      if (!/^\d+$/.test(raw) || Number(raw) < 1 || Number(raw) > 65535) {
        throw new Error(`Invalid port: ${raw}`)
      }
      port = raw
      index += 1
    } else if (token === '--no-open') {
      if (mode !== 'web') throw new Error('--no-open is only valid in web mode')
      noOpen = true
    } else if (mode === 'web' && token === '--patch') {
      passthrough.push(token, takeValue(argv, index, token))
      index += 1
    } else if (mode === 'web' && ['--help', '--dump-config', '--dump-default-config'].includes(token)) {
      passthrough.push(token)
    } else if (token.startsWith('--') && mode !== 'plugin' && mode !== 'headless') {
      throw new Error(`Unknown option: ${token}`)
    } else {
      passthrough.push(token)
    }
  }

  if ((mode === 'headless' || mode === 'plugin') && !workspace) {
    throw new Error(`${mode} mode requires an explicit --workspace`)
  }
  if (mode === 'doctor' && (workspace || passthrough.length > 0)) {
    throw new Error('doctor does not accept a workspace or positional arguments')
  }

  const upstreamRoot = join(repoRoot, 'third_party/deepseek-harness')
  const childEnv = Object.fromEntries(
    Object.entries(env).filter(([key]) => SAFE_ENV_KEYS.has(key)),
  )
  if (corePath) {
    for (const key of ['DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'DB_TIMEZONE', 'JWT_SECRET', 'JWT_EXPIRES_IN', 'BCRYPT_SALT_ROUNDS']) {
      if (env[key] !== undefined) childEnv[key] = env[key]
    }
  }
  childEnv.DSH_HOME = dataRoot
  childEnv.DSH_TELEMETRY_DISABLED = '1'

  let args
  if (mode === 'web') {
    args = ['web', ...passthrough]
    if (port !== undefined) args.push('--port', port)
    if (noOpen) args.push('--no-open')
  } else if (mode === 'headless') {
    args = ['--profile', 'headless', ...passthrough]
  } else if (mode === 'plugin') {
    args = ['plugin', ...passthrough]
  } else {
    args = []
  }

  return {
    mode,
    dataRoot,
    workspace,
    corePath,
    upstreamRoot,
    args,
    env: childEnv,
    ...(mode === 'doctor' ? inspectBuild(upstreamRoot) : {}),
  }
}

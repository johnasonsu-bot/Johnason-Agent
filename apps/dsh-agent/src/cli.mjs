#!/usr/bin/env node
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { resolveLaunch } from './launch-config.mjs'
export { prepareProfile } from './profile.mjs'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')

async function main() {
  try {
    const launch = resolveLaunch(process.argv.slice(2), {
      homeDir: homedir(),
      repoRoot,
      env: process.env,
      nodeVersion: process.versions.node,
    })

    if (launch.mode === 'doctor') {
      if (launch.built) {
        process.stdout.write('DSH native build artifacts are ready.\n')
      } else {
        process.stderr.write(`${launch.diagnostics.join('\n')}\n`)
        process.exitCode = 1
      }
    } else {
      const require = createRequire(resolve(launch.upstreamRoot, 'package.json'))
      const hook = require.resolve('tsx/esm')
      await mkdir(launch.dataRoot, { recursive: true, mode: 0o700 })
      const child = spawn(process.execPath, ['--import', hook, fileURLToPath(new URL('./native-runner.mjs', import.meta.url)), JSON.stringify({ ...launch, env: undefined })], {
        cwd: launch.workspace ?? launch.dataRoot,
        env: { ...launch.env, TSX_TSCONFIG_PATH: resolve(launch.upstreamRoot, 'tsconfig.json') },
        stdio: 'inherit',
      })
      const terminate = () => child.kill('SIGTERM')
      const interrupt = () => child.kill('SIGINT')
      process.on('SIGTERM', terminate); process.on('SIGINT', interrupt)
      process.exitCode = await new Promise((done, reject) => {
        child.once('error', reject)
        child.once('exit', (code, signal) => done(code ?? (signal === 'SIGINT' ? 130 : 1)))
      })
      process.off('SIGTERM', terminate); process.off('SIGINT', interrupt)
    }
  } catch (error) {
    const code = error?.code ? `${error.code}: ` : ''
    process.stderr.write(`${code}${error instanceof Error ? error.message : String(error)}\n`)
    process.exitCode = 1
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}

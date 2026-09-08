#!/usr/bin/env node
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { resolveLaunch } from './launch-config.mjs'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')

export function prepareProfile() {
  throw Object.assign(
    new Error('Encrypted standalone profile is not ready; execution is disabled until the credentials provider is installed.'),
    { code: 'PROFILE_NOT_READY' },
  )
}

function main() {
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
      prepareProfile(launch)
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

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import type { ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { once } from 'node:events'

const helper = join(dirname(createRequire(import.meta.url).resolve('dsh-plugin-subscriptions/package.json')), 'native/keychain-helper')
function start(account: string): { child: ChildProcessWithoutNullStreams; ready: Promise<string> } {
  const child = spawn(helper, ['lock', account], { stdio: ['pipe', 'pipe', 'pipe'] })
  const ready = new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error('ownership helper timeout')) }, 5000)
    child.once('error', reject)
    child.stdout.once('data', data => { clearTimeout(timer); resolve(String(data).trim()) })
    child.once('exit', code => { clearTimeout(timer); resolve(`exit:${code}`) })
  })
  return { child, ready }
}

test('one credential-store owner excludes another process until the first host exits', { skip: process.platform !== 'darwin' }, async () => {
  const account = randomBytes(32).toString('hex')
  const first = start(account)
  try {
    assert.equal(await first.ready, 'locked', 'the native helper must hold an OS-backed lifetime lock')
    const second = start(account)
    assert.equal(await second.ready, 'exit:75', 'a second process must fail closed before reading or refreshing credentials')
    const different = start(randomBytes(32).toString('hex'))
    try { assert.equal(await different.ready, 'locked') }
    finally { different.child.stdin.end() }
    const exited = once(first.child, 'exit')
    first.child.stdin.end()
    await exited
    const next = start(account)
    try { assert.equal(await next.ready, 'locked', 'kernel releases ownership after owner exits without deleting the lock file') }
    finally { next.child.stdin.end() }
  } finally { first.child.stdin.end() }
})

test('an acquired owner that exits cannot be reacquired by the same host', async (t) => {
  const { default: cp } = await import('node:child_process')
  const { EventEmitter } = await import('node:events')
  const { PassThrough } = await import('node:stream')
  const { syncBuiltinESMExports } = await import('node:module')
  const { KeychainSessionStorage } = await import('../src/auth/secret-storage.js')
  const children: (InstanceType<typeof EventEmitter> & { exitCode: number | null; signalCode: string | null })[] = []
  let spawns = 0
  t.mock.method(cp, 'spawn', (() => {
    spawns++
    const child = Object.assign(new EventEmitter(), {
      exitCode: null as number | null, signalCode: null as string | null, killed: false,
      stdin: Object.assign(new PassThrough(), { unref() {} }),
      stdout: Object.assign(new PassThrough(), { unref() {} }),
      unref() {}, kill() { return true },
    })
    children.push(child)
    queueMicrotask(() => child.stdout.emit('data', Buffer.from('locked\n')))
    return child
  }) as unknown as typeof cp.spawn)
  t.mock.method(cp, 'spawnSync', (() => ({ status: 44, stdout: '', stderr: '' })) as unknown as typeof cp.spawnSync)
  syncBuiltinESMExports()
  try {
    const storage = new KeychainSessionStorage()
    const namespace = `/private/tmp/lock-lost-${randomBytes(12).toString('hex')}/auth.json`
    assert.equal(await storage.read(namespace), undefined)
    children[0].exitCode = 0
    children[0].emit('exit', 0)
    await assert.rejects(storage.read(namespace), /STORE_LOCK_LOST/)
    assert.equal(spawns, 1, 'an in-flight refresh may not cross a lock loss and acquire ownership again')
  } finally {
    t.mock.restoreAll()
    syncBuiltinESMExports()
  }
})

/** Local credential backend. Default is macOS Keychain; there is no file fallback. */
import { AsyncLocalStorage } from 'node:async_hooks'
import { createHash } from 'node:crypto'
import { spawn, spawnSync } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import type { Socket } from 'node:net'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'

export interface SessionStorage {
  read(namespace: string): Promise<string | undefined>
  write(namespace: string, serialized: string): Promise<void>
}

export interface NativeResult {
  status: number | null
  stdout: string
  error?: unknown
}
export type NativeRunner = (operation: 'read' | 'write', account: string, input?: string, owner?: string) => NativeResult | Promise<NativeResult>

/** Keychain errors expose only an action code, never command output or token input. */
export class KeychainSessionStorage implements SessionStorage {
  constructor(private readonly runner: NativeRunner = invokeNative) {}
  private account(namespace: string): string {
    return createHash('sha256').update(resolve(namespace)).digest('hex')
  }
  private owner(namespace: string): string {
    return this.account(dirname(resolve(namespace)))
  }
  async read(namespace: string): Promise<string | undefined> {
    const result = await this.runner('read', this.account(namespace), undefined, this.owner(namespace))
    if (result.error !== undefined) throw new Error('SUBSCRIPTIONS_KEYCHAIN_UNAVAILABLE: build the native helper and unlock macOS Keychain')
    if (result.status === 44) return undefined
    if (result.status !== 0) throw new Error('SUBSCRIPTIONS_KEYCHAIN_UNAVAILABLE: unlock macOS Keychain; plaintext fallback is disabled')
    return result.stdout
  }
  async write(namespace: string, serialized: string): Promise<void> {
    const result = await this.runner('write', this.account(namespace), serialized, this.owner(namespace))
    if (result.error !== undefined || result.status !== 0) {
      throw new Error('SUBSCRIPTIONS_KEYCHAIN_WRITE_FAILED: unlock macOS Keychain; plaintext fallback is disabled')
    }
  }
}

function helperPath(): string {
  const require = createRequire(import.meta.url)
  return join(dirname(require.resolve('dsh-plugin-subscriptions/package.json')), 'native/keychain-helper')
}

const owners = new Map<string, Promise<ChildProcess>>()
const lostOwners = new Set<string>()
/** Only one DSH host may read/refresh/mutate one store at a time. */
async function ownStore(owner: string): Promise<void> {
  if (lostOwners.has(owner)) throw new Error('SUBSCRIPTIONS_STORE_LOCK_LOST: restart this DSH host before using credentials')
  let pending = owners.get(owner)
  if (pending === undefined) {
    pending = new Promise<ChildProcess>((resolveOwner, reject) => {
      const child = spawn(helperPath(), ['lock', owner], { stdio: ['pipe', 'pipe', 'ignore'] })
      let acquired = false
      const fail = (message: string) => reject(new Error(message))
      const timer = setTimeout(() => { child.stdin?.end(); child.kill(); fail('SUBSCRIPTIONS_STORE_LOCK_UNAVAILABLE') }, 5000)
      child.once('error', () => { clearTimeout(timer); fail('SUBSCRIPTIONS_STORE_LOCK_UNAVAILABLE') })
      child.once('exit', code => {
        clearTimeout(timer)
        if (acquired) lostOwners.add(owner)
        else owners.delete(owner)
        fail(code === 75
          ? 'SUBSCRIPTIONS_STORE_IN_USE: close the other DSH host before using this credential store'
          : 'SUBSCRIPTIONS_STORE_LOCK_UNAVAILABLE')
      })
      child.stdout?.once('data', data => {
        clearTimeout(timer)
        if (String(data).trim() !== 'locked') { child.stdin?.end(); fail('SUBSCRIPTIONS_STORE_LOCK_UNAVAILABLE'); return }
        // The helper must not keep Node alive. Process exit closes stdin and
        // the kernel releases flock; the empty lock file is retained.
        acquired = true
        child.unref()
        ;(child.stdin as Socket | null)?.unref()
        ;(child.stdout as Socket | null)?.unref()
        resolveOwner(child)
      })
    })
    owners.set(owner, pending)
  }
  try {
    const child = await pending
    if (child.exitCode !== null || child.signalCode !== null || child.killed) {
      lostOwners.add(owner)
      throw new Error('SUBSCRIPTIONS_STORE_LOCK_LOST: restart this DSH host before using credentials')
    }
  } catch (error) {
    owners.delete(owner)
    throw error
  }
}

async function invokeNative(operation: 'read' | 'write', account: string, input?: string, owner = account): Promise<NativeResult> {
  if (process.platform !== 'darwin') return { status: 78, stdout: '' }
  await ownStore(owner)
  const result = spawnSync(helperPath(), [operation, account], {
    encoding: 'utf8', input: input ?? '', timeout: 5000, maxBuffer: 4 * 1024 * 1024,
    stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  })
  // Do not propagate stderr/spawn error objects: they can contain arguments or input.
  return { status: result.status, stdout: result.stdout ?? '', ...(result.error ? { error: true } : {}) }
}

const defaultStorage = new KeychainSessionStorage()
const contexts = new AsyncLocalStorage<SessionStorage>()
/** Scope a host-provided secure backend to one asynchronous operation. No global/env switch. */
export function withSessionStorage<T>(storage: SessionStorage, action: () => T): T {
  return contexts.run(storage, action)
}
export function sessionStorage(): SessionStorage {
  return contexts.getStore() ?? defaultStorage
}

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, existsSync, readdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { saveAccountSession, listAccounts, loadStore, deleteAccountSession } from '../src/auth/store.js'
import { KeychainSessionStorage, withSessionStorage } from '../src/auth/secret-storage.js'
import type { SessionStorage, NativeRunner } from '../src/auth/secret-storage.js'
import { writeBackClaudeCodeCredentials } from '../src/auth/claude-code-creds.js'

class MemoryBackend implements SessionStorage {
  values = new Map<string, string>()
  async read(namespace: string) { return this.values.get(namespace) }
  async write(namespace: string, value: string) { this.values.set(namespace, value) }
}
const fixture = {
  accessToken: 'fixture-access', refreshToken: 'fixture-refresh',
  expiresAt: 2_000_000_000_000, accountId: 'fixture-account',
}

test('saving a subscription session must never create a plaintext auth file', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'johnason-secure-store-'))
  const path = join(dir, 'auth.json')
  await withSessionStorage(new MemoryBackend(), async () => {
    await saveAccountSession('codex', 'fixture-account', fixture, path)
    assert.equal((await listAccounts('codex', path))[0]?.session.refreshToken, 'fixture-refresh')
    assert.equal(existsSync(path), false, 'OAuth tokens must not be persisted to auth.json')
    assert.deepEqual(readdirSync(dir), [], 'no temporary plaintext credential files may remain')
    await deleteAccountSession('codex', 'fixture-account', path)
    assert.deepEqual(await listAccounts('codex', path), [])
  })
})

test('legacy plaintext credentials are not read, migrated, modified or removed implicitly', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'johnason-legacy-auth-'))
  const path = join(dir, 'auth.json')
  writeFileSync(path, JSON.stringify({ codex: fixture }))
  await withSessionStorage(new MemoryBackend(), async () => assert.deepEqual(await loadStore(path), {}))
  assert.equal(existsSync(path), true)
})

test('concurrent account writes preserve every account in the secure backend', async () => {
  await withSessionStorage(new MemoryBackend(), async () => {
    await Promise.all(Array.from({ length: 12 }, (_, i) => saveAccountSession('codex', `account-${i}`,
      { ...fixture, accountId: `account-${i}` }, '/isolated/concurrent-auth')))
    assert.equal((await listAccounts('codex', '/isolated/concurrent-auth')).length, 12)
  })
})

test('different namespaces and asynchronous backend scopes do not share sessions', async () => {
  const a = new MemoryBackend(), b = new MemoryBackend()
  await withSessionStorage(a, async () => {
    await saveAccountSession('codex', 'fixture-account', fixture, '/isolated/a')
    assert.deepEqual(await loadStore('/isolated/b'), {})
    await withSessionStorage(b, async () => assert.deepEqual(await loadStore('/isolated/a'), {}))
    assert.equal((await listAccounts('codex', '/isolated/a')).length, 1)
  })
})

test('only a Keychain not-found response means an empty store', async () => {
  const missing = new KeychainSessionStorage(() => ({ status: 44, stdout: '' }))
  assert.equal(await missing.read('/isolated/a'), undefined)
  for (const status of [73, 70, 78, null]) {
    const denied = new KeychainSessionStorage(() => ({ status, stdout: 'sensitive-output' }))
    await assert.rejects(denied.read('/isolated/a'), error => {
      assert.match(String(error), /SUBSCRIPTIONS_KEYCHAIN_UNAVAILABLE/)
      assert.doesNotMatch(String(error), /sensitive-output/)
      return true
    })
  }
})

test('write errors never create a plaintext fallback and never expose native output', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'johnason-denied-keychain-'))
  const path = join(dir, 'auth.json')
  const denied = new KeychainSessionStorage((op) => op === 'read'
    ? { status: 44, stdout: '' } : { status: 73, stdout: 'sensitive-output' })
  await withSessionStorage(denied, async () => {
    await assert.rejects(saveAccountSession('codex', 'fixture-account', fixture, path), error => {
      assert.match(String(error), /SUBSCRIPTIONS_KEYCHAIN_WRITE_FAILED/)
      assert.doesNotMatch(String(error), /fixture-access|sensitive-output/)
      return true
    })
  })
  assert.deepEqual(readdirSync(dir), [])
})

test('a spawned-helper failure is not confused with an empty store', async () => {
  const backend = new KeychainSessionStorage(() => ({ status: 44, stdout: '', error: new Error('sensitive-error') }))
  await assert.rejects(backend.read('/isolated/a'), error => {
    assert.match(String(error), /SUBSCRIPTIONS_KEYCHAIN_UNAVAILABLE/)
    assert.doesNotMatch(String(error), /sensitive-error/)
    return true
  })
})

test('native calls carry only a hashed namespace in arguments and the session in stdin', async () => {
  const calls: Parameters<NativeRunner>[] = []
  const backend = new KeychainSessionStorage((...args) => {
    calls.push(args)
    return { status: 0, stdout: args[0] === 'read' ? 'saved-value' : '' }
  })
  await backend.write('/private/account/namespace', 'private-session-json')
  assert.equal(calls[0][0], 'write')
  assert.match(calls[0][1], /^[a-f0-9]{64}$/)
  assert.equal(calls[0][2], 'private-session-json')
  assert.equal(await backend.read('/private/account/namespace'), 'saved-value')
  assert.equal(calls[1][1], calls[0][1])
  assert.equal(calls[1][2], undefined)
})

test('malformed Keychain data fails without logging its plaintext content', async () => {
  const backend = new MemoryBackend()
  backend.values.set('/isolated/corrupt', 'sensitive-corrupt-value')
  await withSessionStorage(backend, async () => {
    await assert.rejects(loadStore('/isolated/corrupt'), error => {
      assert.match(String(error), /not valid JSON/)
      assert.doesNotMatch(String(error), /sensitive-corrupt-value/)
      return true
    })
  })
})

test('subscription refresh cannot write into another CLI credential store', () => {
  assert.equal(writeBackClaudeCodeCredentials({ ...fixture, scopes: 'fixture' }, 'fixture-access'), false)
})

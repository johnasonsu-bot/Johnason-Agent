import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { withSessionStorage, KeychainSessionStorage } from '../src/auth/secret-storage.js'
import { proxySetConfig, proxyGetConfig, resetProxyForTests, parseProxyUrl } from '../src/http.js'

async function isolated<T>(run: (dir: string, values: Map<string, string>) => Promise<T>) {
  const dir = mkdtempSync(join(tmpdir(), 'johnason-secure-proxy-'))
  const old = process.env.DSH_HOME
  const values = new Map<string, string>()
  process.env.DSH_HOME = dir
  await resetProxyForTests()
  try {
    return await withSessionStorage({ async read(key) { return values.get(key) }, async write(key, data) { values.set(key, data) } }, () => run(dir, values))
  } finally {
    await resetProxyForTests()
    if (old === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = old
  }
}

test('proxy credentials persist only in the secure backend and views omit passwords', async () => {
  await isolated(async (dir, values) => {
    const view = await proxySetConfig({ enabled: false, url: 'http://localhost:8888', username: 'fixture-user', password: 'fixture-password' })
    assert.equal(view.passwordSet, true)
    assert.doesNotMatch(JSON.stringify(view), /fixture-password/)
    assert.deepEqual(readdirSync(dir), [], 'proxy.json must not persist credentials')
    assert.equal([...values.values()].some(value => value.includes('fixture-password')), true)
    await resetProxyForTests()
    assert.equal((await proxyGetConfig()).passwordSet, true)
    await proxySetConfig({ enabled: false, url: 'http://localhost:8888', password: null })
    assert.equal((await proxyGetConfig()).passwordSet, false)
  })
})

test('proxy URLs reject embedded userinfo, query and fragments without echoing them', () => {
  for (const url of ['http://fixture-user:fixture-secret@localhost:8888', 'http://localhost:8888?token=fixture-secret',
    'http://localhost:8888#fixture-secret', 'socks5://fixture-user:fixture-secret@localhost:8888', 'fixture-secret']) {
    assert.throws(() => parseProxyUrl(url), error => {
      assert.doesNotMatch(String(error), /fixture-secret/)
      return true
    })
  }
})

test('a locked proxy credential store fails closed instead of silently routing directly', async () => {
  await resetProxyForTests()
  try {
    await withSessionStorage(new KeychainSessionStorage(() => ({ status: 73, stdout: '' })), async () => {
      await assert.rejects(proxyGetConfig(), /KEYCHAIN_UNAVAILABLE/)
    })
  } finally { await resetProxyForTests() }
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Context } from '@deepseek-ai/cordis'
import { mkdtempSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import * as plugin from '../src/index.js'
import { withSessionStorage } from '../src/auth/secret-storage.js'
import { createFakeConnection } from './fake-connection.js'

test('configured providers also gate status and every inactive-provider auth operation', async () => {
  const old = process.env.DSH_HOME
  process.env.DSH_HOME = mkdtempSync(join(tmpdir(), 'johnason-provider-gates-'))
  const ctx = new Context()
  const fake = createFakeConnection()
  ctx.provide('llm', { registerAdapter: () => Object.assign(() => {}, { replace: () => {} }) })
  ctx.provide('connection', fake.connection)
  const values = new Map<string, string>()
  let dispose = async () => {}
  try {
    await withSessionStorage({ async read(key) { return values.get(key) }, async write(key, value) { values.set(key, value) } }, async () => {
      const runtime = ctx.plugin(plugin, { providers: ['codex'], codexClientVersion: '1.0.0', pool: { enabled: false } })
      dispose = () => runtime.dispose()
      await new Promise(resolve => setTimeout(resolve, 70))
      if (!fake.registered()) await runtime
      const status = await fake.handler('status', {}, new AbortController().signal)
      assert.equal(status.ok, true)
      if (!status.ok) return
      const providers = (status.value as { providers: Record<string, { disabled?: boolean; accounts: unknown[] }> }).providers
      assert.equal(providers.claude.disabled, true, 'a nonconfigured provider must be marked disabled without importing its CLI credentials')
      assert.equal(providers.codex.disabled, undefined)
      for (const provider of ['claude', 'grok', 'copilot', 'antigravity']) {
        assert.deepEqual(providers[provider].accounts, [])
        for (const endpoint of ['login', 'manual', 'cancel', 'logout', 'setDefault', 'usage']) {
          const result = await fake.handler(endpoint, { provider, method: provider === 'claude' ? 'keychain' : 'oauth', account: 'fixture', input: 'fixture' }, new AbortController().signal)
          assert.equal(result.ok, false, `${endpoint} must reject disabled ${provider}`)
        }
      }
    })
  } finally {
    await dispose()
    if (old === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = old
  }
})

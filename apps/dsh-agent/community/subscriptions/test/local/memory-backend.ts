import type { SessionStorage } from '../../src/auth/secret-storage.js'
/** Test-only backend: no production/environment switch can enable it. */
export const fixtureSecrets = new Map<string, string>()
export const fixtureBackend: SessionStorage = {
  async read(namespace) { return fixtureSecrets.get(namespace) },
  async write(namespace, value) { fixtureSecrets.set(namespace, value) },
}
export function seedCredentialFixture(path: string, value: string, _options?: unknown): void {
  fixtureSecrets.set(path, value)
}
export function readCredentialFixture(path: string, _encoding?: unknown): string {
  const value = fixtureSecrets.get(path)
  if (value === undefined) throw new Error('missing credential fixture')
  return value
}

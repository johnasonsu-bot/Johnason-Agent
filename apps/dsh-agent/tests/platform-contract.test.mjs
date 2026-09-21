import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('upgrade contract refuses revision, version and package-manager disagreement', async () => {
  const api = await import('../scripts/check-platform.mjs').catch(() => ({}));
  assert.equal(typeof api.assertReleaseAgreement, 'function');
  const release = { revision: 'a'.repeat(40), version: '0.1.6-alpha.2', packageManager: 'pnpm@11.7.0' };
  const actual = { head: release.revision, gitlink: release.revision, version: release.version, packageManager: release.packageManager };
  assert.doesNotThrow(() => api.assertReleaseAgreement(release, actual));
  for (const key of Object.keys(actual)) assert.throws(() => api.assertReleaseAgreement(release, { ...actual, [key]: 'mismatched' }), /platform contract/i);
});

test('dependency boundary audit rejects a plugin importing official internals', async () => {
  const api = await import('../scripts/check-platform.mjs').catch(() => ({}));
  assert.equal(typeof api.auditBoundaries, 'function');
  const root = mkdtempSync(join(tmpdir(), 'dsh-boundary-'));
  mkdirSync(join(root, 'apps/dsh-agent/src/plugins'), { recursive: true });
  mkdirSync(join(root, 'apps/dsh-agent/src/platform'), { recursive: true });
  const dependency = "import Core from '@deepseek-ai/dsh-session';";
  writeFileSync(join(root, 'apps/dsh-agent/src/platform/approved.mjs'), dependency);
  assert.deepEqual(api.auditBoundaries(root), []);
  writeFileSync(join(root, 'apps/dsh-agent/src/plugins/violation.mjs'), dependency);
  assert.deepEqual(api.auditBoundaries(root), ['apps/dsh-agent/src/plugins/violation.mjs']);
});

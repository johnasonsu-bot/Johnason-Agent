import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';

const groups = {
  credentials: ['credentials-plugin', 'vault-store', 'vault-lock', 'vault-ui', 'browser-session-record'],
  memory: ['memory-recovery-plugin', 'memory-recovery-ui', 'memory-store', 'memory-pager', 'memory-tools', 'effect-store', 'record-sanitizer', 'task-sandbox'],
  systems: ['systems', 'systems-plugin', 'systems-ui', 'dataplatform', 'dataplatform-plugin', 'dataplatform-ui', 'dataplatform-cli'],
};
const src = new URL('../src/', import.meta.url);

test('browser plugin declares available native client entries', () => {
  const manifest = JSON.parse(readFileSync(new URL('../browser/package.json', import.meta.url), 'utf8'));
  for (const name of manifest.dsh.client.inject) {
    assert.match(name, /^@deepseek-ai\/dsh-client-[a-z-]+$/);
    const directory = name.slice('@deepseek-ai/dsh-client-'.length);
    const dependency = JSON.parse(readFileSync(new URL(`../../../third_party/deepseek-harness/packages/client/${directory}/package.json`, import.meta.url), 'utf8'));
    assert.equal(dependency.name, name);
    assert.equal(dependency.dsh?.client?.platform, 'web', `${name} must be an active browser entry`);
  }
});

test('local plugin implementations have explicit homes and no direct native dependency resolution', () => {
  for (const [group, names] of Object.entries(groups)) {
    for (const name of names) {
      const path = new URL(`plugins/${group}/${name}.mjs`, src);
      assert.ok(existsSync(path), `${name} belongs in plugins/${group}`);
      const source = readFileSync(path, 'utf8');
      assert.doesNotMatch(source, /third_party\/deepseek-harness|@deepseek-ai\//, `${name} must use the platform SDK`);
      const compatibility = readFileSync(new URL(`${name}.mjs`, src), 'utf8');
      assert.match(compatibility, new RegExp(`export \\* from ['"]\\./plugins/${group}/${name}\\.mjs['"]`));
      assert.doesNotMatch(compatibility, /^(?:import|const|let|function|class) /m, `${name} old entry must only forward exports`);
    }
  }
});

test('old public imports preserve every export and class identity after relocation', async () => {
  // Loading framework types requires the native build. This contract only tests
  // ESM forwarding identity; behavioral tests exercise the actual plugin logic.
  const boundary = `data:text/javascript,${encodeURIComponent(`
    export class CredentialProvider {};
    export class Service {};
    export class LlmError extends Error {};
    export const credentialRef = value => value;
    export const parseCredentialKey = value => value;
    export const rootContext = ctx => ctx;
    export const createUserMessage = value => value;
    export const installMemoryToolExecutor = () => () => {};
    export const retainRequiredEventTypes = () => () => {};
    export const snapshotSessionEvents = session => session.snapshotEvents();
  `)}`;
  const hooks = registerHooks({ resolve(specifier, context, next) {
    if (/\/platform\/(credential|memory)-api\.mjs$/.test(new URL(specifier, context.parentURL).pathname)) return { url: boundary, shortCircuit: true };
    return next(specifier, context);
  } });
  try {
    for (const [group, names] of Object.entries(groups)) {
      for (const name of names) {
        const oldEntry = await import(new URL(`${name}.mjs`, src));
        const implementation = await import(new URL(`plugins/${group}/${name}.mjs`, src));
        assert.deepEqual(Object.keys(oldEntry), Object.keys(implementation), name);
        for (const key of Object.keys(implementation)) assert.equal(oldEntry[key], implementation[key], `${name}.${key}`);
      }
    }
  } finally { hooks.deregister(); }
});

test('relocated plugin pages serve the original single-copy browser resources', async () => {
  const { createVaultHandler } = await import('../src/vault-ui.mjs');
  const { createMemoryRecoveryHandler } = await import('../src/memory-recovery-ui.mjs');
  const { createSystemsHandler } = await import('../src/systems-ui.mjs');
  const { createDataPlatformHandler } = await import('../src/dataplatform-ui.mjs');
  for (const [route, handler] of [
    ['vault', createVaultHandler({})],
    ['memory-recovery', createMemoryRecoveryHandler({}, {})],
    ['systems', createSystemsHandler(new Map(), {})],
    ['dataplatform', createDataPlatformHandler({}, {})],
  ]) {
    const request = { method: 'GET', url: `/${route}`, headers: { host: 'localhost:43210' },
      socket: { remoteAddress: '127.0.0.1', localPort: 43210 } };
    let status; let headers; let body;
    await handler(request, { writeHead(value, values) { status = value; headers = values; }, end(value) { body = value; } });
    assert.equal(status, 200, route);
    assert.match(headers['content-type'], /^text\/html/);
    assert.equal(body, readFileSync(new URL(`../public/${route}.html`, import.meta.url), 'utf8'));
  }
});

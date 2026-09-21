import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { realpath } from 'node:fs/promises';
import { createMemoryRecoveryHandler, installMemoryRecoveryUi } from '../src/memory-recovery-ui.mjs';

// Exercise the actual HTTP handler without starting native Web or requiring built packages.
async function post(handler, action, data = {}, headers = {}) {
  const req = Readable.from([Buffer.from(JSON.stringify(data))]);
  Object.assign(req, { method: 'POST', url: `/memory-recovery/api/${action}`,
    headers: { host: 'localhost:43210', origin: 'http://localhost:43210', 'content-type': 'application/json', ...headers },
    socket: { remoteAddress: '127.0.0.1', localPort: 43210 } });
  let status; let body;
  await handler(req, { writeHead(value) { status = value; }, end(value) { body = JSON.parse(value); } });
  return { status, body };
}

test('memory UI defaults use the captured host working directory without a removed host API', async () => {
  const handler = createMemoryRecoveryHandler({}, {}, { cwd: '/host-workspace' });
  assert.deepEqual(await post(handler, 'defaults'), { status: 200, body: { value: { workspaceRoot: '/host-workspace' } } });
});

test('memory UI creates an operator-selected workspace with direct native business arguments', async () => {
  const workspaceRoot = await realpath(new URL('..', import.meta.url));
  const calls = [];
  const handler = createMemoryRecoveryHandler({}, {
    async create(request) { calls.push(request); return { sessionId: request.sessionId }; },
  });
  const result = await post(handler, 'create-session', { workspaceRoot });
  assert.equal(result.status, 200);
  assert.match(result.body.value.sessionId, /^session-[0-9a-f-]{36}$/);
  assert.deepEqual(calls, [{ sessionId: result.body.value.sessionId, cwd: workspaceRoot }]);
  assert.equal(result.body.value.workspaceRoot, workspaceRoot);
});

test('memory UI reattaches only proven root history and shares concurrent attachment', async () => {
  let attached = false; let listed = 0; const creates = [];
  const config = { sessionId: 'saved-root', version: 1, enabled: true };
  const handler = createMemoryRecoveryHandler({ getSessionConfig(id) {
    assert.equal(id, 'saved-root');
    if (!attached) throw Object.assign(new Error('not attached'), { code: 'MEMORY_SESSION_NOT_FOUND' });
    return config;
  } }, {
    async list(request, signal) {
      listed++; assert.deepEqual(request, {}); assert.ok(signal instanceof AbortSignal);
      return { items: [{ sessionId: 'saved-root', cwd: '/saved-workspace', running: false }] };
    },
    async create(request) { creates.push(request); await new Promise(resolve => setImmediate(resolve)); attached = true; return { sessionId: request.sessionId }; },
  });
  const results = await Promise.all([post(handler, 'config', { sessionId: 'saved-root' }), post(handler, 'config', { sessionId: 'saved-root' })]);
  assert.deepEqual(results, [0, 1].map(() => ({ status: 200, body: { value: config } })));
  assert.equal(listed, 1);
  assert.deepEqual(creates, [{ sessionId: 'saved-root', cwd: '/saved-workspace' }]);
});

test('memory UI refuses unknown, subagent and nonabsolute historical identities without creating anything', async () => {
  for (const row of [undefined, { sessionId: 'old', cwd: '/saved', origin: 'subagent' }, { sessionId: 'old', cwd: 'relative' }]) {
    const handler = createMemoryRecoveryHandler({ getSessionConfig() { throw Object.assign(new Error('missing'), { code: 'MEMORY_SESSION_NOT_FOUND' }); } }, {
      async list() { return { items: row ? [row] : [] }; },
      async create() { assert.fail('unproven history must not be created'); },
    });
    assert.equal((await post(handler, 'config', { sessionId: 'old' })).body.code, 'MEMORY_SESSION_NOT_FOUND');
  }
});

test('memory UI contains direct native creation exceptions and unexpected identities', async () => {
  const workspaceRoot = await realpath(new URL('..', import.meta.url));
  for (const create of [async () => { throw new Error('native-private-detail'); }, async () => ({ sessionId: 'different-existing-id' })]) {
    const result = await post(createMemoryRecoveryHandler({}, { create }), 'create-session', { workspaceRoot });
    assert.equal(result.status, 400);
    assert.equal(result.body.code, 'MEMORY_NATIVE_CREATE_FAILED');
    assert.equal(JSON.stringify(result).includes('native-private-detail'), false);
  }
});

test('memory UI rejects foreign origins before invoking native operations', async () => {
  const handler = createMemoryRecoveryHandler({}, { create() { assert.fail('foreign request reached native controller'); } });
  assert.equal((await post(handler, 'create-session', { workspaceRoot: '/tmp' }, { origin: 'https://foreign.invalid' })).status, 403);
});

test('memory UI installs against the native session controller without apiProxy', async () => {
  let route;
  installMemoryRecoveryUi({ sessionController: {}, effect: setup => setup(), webServer: { register(value) { route = value; } } }, {});
  assert.equal(route.path, '/memory-recovery');
  assert.equal(route.kind, 'prefix');
  assert.equal((await post(route.handler, 'defaults')).status, 200);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks, stripTypeScriptTypes } from 'node:module';
import { mkdtempSync, readFileSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Native packages are deliberately not built in source-only verification. Replace
// only their loading/framework boundary; execute the real plugin, pager and SQLite
// stores. Pure native surface/format validators run directly from the pinned
// TypeScript sources in memory; routing and sandbox dispatch remain integration tests.
const pluginUrl = new URL('../src/memory-recovery-plugin.mjs', import.meta.url).href;
const memoryApiUrl = new URL('../src/platform/memory-api.mjs', import.meta.url).href;
const stub = source => `data:text/javascript,${encodeURIComponent(source)}`;
const native = path => new URL(`../../../third_party/deepseek-harness/packages/${path}`, import.meta.url).href;
const modules = {
  cordis: stub('export class Service { constructor(ctx) { this.ctx = ctx; } }'),
  session: native('core/session/src/known-event-types.ts'),
  executor: stub('export class NativeToolExecutor {}'),
  llm: stub('let id = 0; export const createUserMessage = value => ({ id: `source-message-${++id}`, role: "user", ...value });'),
};
const nativeAliases = {
  '@deepseek-ai/dsh-brand': native('util/brand/src/index.ts'),
  '@deepseek-ai/dsh-util-values': native('util/values/src/index.ts'),
  '@deepseek-ai/dsh-session-format': native('session/session-format/src/index.ts'),
  '@deepseek-ai/dsh-session-format-v0-to-v1': native('session/session-format-v0-to-v1/src/index.ts'),
  '@deepseek-ai/dsh-session-format-v1-to-v2': stub(`export * from ${JSON.stringify(native('session/session-format-v1-to-v2/src/validation.ts'))}; export * from ${JSON.stringify(native('session/session-format-v1-to-v2/src/dispositions.ts'))};`),
};
const moduleBridge = stub(`export function nativeRequire() { return { resolve(name) { if (name === '@deepseek-ai/cordis') return ${JSON.stringify(modules.cordis)}; throw new Error('Unexpected native dependency: ' + name); } }; } export function nativeUrl(path) { if (path === 'packages/core/tool-executor-native/lib/index.js') return ${JSON.stringify(modules.executor)}; if (path === 'packages/llm/llm/lib/index.js') return ${JSON.stringify(modules.llm)}; throw new Error('Unexpected native path: ' + path); }`);
const hooks = registerHooks({ resolve(specifier, context, next) {
  if (context.parentURL === memoryApiUrl) {
    if (specifier === './runtime.mjs') return { url: moduleBridge, shortCircuit: true };
    if (specifier === '@deepseek-ai/dsh-session') return { url: modules.session, shortCircuit: true };
  }
  if (nativeAliases[specifier]) return { url: nativeAliases[specifier], shortCircuit: true };
  return next(specifier, context);
}, load(url, context, next) {
  if (url.startsWith(native('')) && url.endsWith('.ts')) return { format: 'module',
    source: stripTypeScriptTypes(readFileSync(new URL(url), 'utf8'), { mode: 'transform', sourceUrl: url }), shortCircuit: true };
  return next(url, context);
} });
let memoryPlugin, SurfaceManager, restoreReleasedV3Artifact, assertReleasedEventPayload, KNOWN_SESSION_EVENT_TYPES, retainRequiredEventTypes, snapshotSessionEvents;
try {
  memoryPlugin = await import(pluginUrl);
  ({ retainRequiredEventTypes, snapshotSessionEvents } = await import(memoryApiUrl));
  ({ SurfaceManager } = await import(native('core/session/src/surface.ts')));
  ({ restoreReleasedV3Artifact } = await import(native('session/session-format-v2-to-v3/src/validation.ts')));
  ({ assertReleasedEventPayload } = await import(native('session/session-format-v0-to-v1/src/validation.ts')));
  ({ KNOWN_SESSION_EVENT_TYPES } = await import(modules.session));
} finally { hooks.deregister(); }

function fixture(t, options = {}) {
  const root = mkdtempSync(join(tmpdir(), 'dsh-memory-source-'));
  const listeners = new Map(); const cleanups = []; const operations = []; let stored = [];
  const events = options.events ?? [];
  const session = { id: 'source-session', header: { version: 3, id: 'source-session', createdAt: 1, cwd: root, isSeeded: false, delegationDepth: 0, ...options.header },
    inheritedEventCount: options.inheritedEventCount ?? 0,
    snapshotEvents(from = 0, to = events.length) { return Object.freeze(events.slice(from, to)); },
    get seq() { return events.length; },
    append(type, data, intent = {}) { const event = { type, data, time: 100, seq: this.seq, ...intent };
      this.surface.validateNext(event); events.push(event);
      for (const listener of listeners.get('session/event') ?? []) listener(this, event); return event; },
  };
  session.surface = new SurfaceManager(events);
  const ctx = {
    get(name) { return this[name]; },
    on(name, listener) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(listener); },
    effect(setup) { const cleanup = setup(); if (cleanup) cleanups.push(cleanup); },
    inject(names, setup) { if (names.every(name => this[name])) setup(this); },
    tools: { guard() {}, register() {}, restrict() { return () => {}; } }, systemPrompt: { section() {} },
    webServer: { register(route) { ctx.route = route; } }, sessionController: {}, workspaceController: {}, workspaceRegistry: {},
    sessions: { get: id => id === session.id ? session : undefined, list: () => [session],
      async flush(target) {
        assert.equal(target, session); operations.push('flush');
        if (options.flushError) throw options.flushError;
        stored = structuredClone(session.snapshotEvents());
        await Promise.all((listeners.get('session/flush') ?? []).map(listener => listener(session)));
      },
    },
    sessionPersistence: { async open(id, access) {
      operations.push(['open', id, access]); assert.equal(id, session.id); assert.equal(access, 'read');
      return { id, header: session.header, inheritedEventCount: session.inheritedEventCount, access,
        async read(from) { operations.push(['read', from]); if (options.readError) throw options.readError;
          if (options.validateFormat) restoreReleasedV3Artifact({ header: session.header, inheritedEventCount: session.inheritedEventCount, events: stored }, KNOWN_SESSION_EVENT_TYPES);
          return { eventState: 'detached', events: options.storedEvents ? options.storedEvents(stored, from) : stored.slice(from) }; },
        async close() { operations.push('close'); if (options.closeError) throw options.closeError; },
      };
    } },
  };
  const service = memoryPlugin.apply(ctx, { enabled: true, path: join(root, 'memory.sqlite') });
  t.after(async () => { for (const cleanup of cleanups.reverse()) { try { await cleanup(); } catch { /* expected sticky failure fixtures retain their databases */ } } });
  const configure = options => service.configureSession(session.id, { enabled: true, projectId: 'source-project', agentId: session.id,
    workspaceRoot: root, sandbox: null, budgetTokens: 4000, anchors: [] }, { expectedVersion: 0, ...options });
  const preStep = messages => listeners.get('agent/pre-step')[0]({ agent: { id: session.id, session, ctx }, messages }, async () => ({ kind: 'enter', messages }));
  return { root, ctx, service, session, configure, operations, preStep };
}

test('memory plugin activates its local UI with the current session controller service', t => {
  const f = fixture(t);
  assert.equal(f.ctx.route?.path, '/memory-recovery');
});

test('a seeded empty session cannot first-enable memory even without parent metadata', t => {
  const f = fixture(t, { header: { isSeeded: true } });
  assert.equal(f.service.getSessionConfig(f.session.id).configurationBlockReason, 'MEMORY_NEW_SESSION_REQUIRED');
});

test('workspace preparation failure cannot commit memory configuration and releases the session lock', async t => {
  const f = fixture(t);
  await assert.rejects(f.configure({ beforeCommit: async scope => {
    assert.equal(scope.sessionId, f.session.id);
    assert.equal(scope.workspaceRoot, realpathSync.native(f.root));
    throw Object.assign(new Error('workspace unavailable'), { code: 'MEMORY_NATIVE_WORKSPACE_FAILED' });
  } }), { code: 'MEMORY_NATIVE_WORKSPACE_FAILED' });
  assert.equal(f.service.getSessionConfig(f.session.id).version, 0);
  assert.equal(f.session.snapshotEvents().length, 0);
  await f.configure();
  assert.equal(f.service.getSessionConfig(f.session.id).version, 1);
});

test('configuration excludes concurrent workspace side effects and rechecks lifecycle before committing', async t => {
  const f = fixture(t); let release; let entered;
  const held = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { entered = resolve; });
  const first = f.configure({ beforeCommit: async () => { entered(); await held; } });
  await ready;
  await assert.rejects(f.service.configureSession(f.session.id, { workspaceRoot: '/different-workspace' }, {
    expectedVersion: 0, beforeCommit: () => assert.fail('concurrent request must not associate a different workspace'),
  }), { code: 'MEMORY_SESSION_BUSY' });
  f.session.append('turn/start', { turn: 1 });
  release();
  await assert.rejects(first, { code: 'MEMORY_NEW_SESSION_REQUIRED' });
  assert.equal(f.service.getSessionConfig(f.session.id).version, 0);
  await assert.rejects(f.configure({ beforeCommit: () => assert.fail('history rejection must precede workspace writes') }), { code: 'MEMORY_NEW_SESSION_REQUIRED' });
});

test('restored untouched root setup remains eligible while started and subagent sessions are blocked', t => {
  for (const [options, expected] of [
    [{ events: [{ type: 'session/end-seed', seq: 0, time: 1, data: {} }] }, null],
    [{ header: { origin: 'subagent' } }, 'MEMORY_NEW_SESSION_REQUIRED'],
    [{ header: { parentSession: 'parent' } }, 'MEMORY_NEW_SESSION_REQUIRED'],
    [{ events: [{ type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } }] }, 'MEMORY_NEW_SESSION_REQUIRED'],
  ]) {
    const f = fixture(t, options);
    assert.equal(f.service.getSessionConfig(f.session.id).configurationBlockReason, expected);
  }
});

test('memory audit opens read ownership after the native flush and closes every snapshot', async t => {
  const f = fixture(t); await f.configure();
  assert.deepEqual(f.operations, ['flush', ['open', 'source-session', 'read'], ['read', 0], 'close']);
  assert.equal(f.service.status(f.session.id).durableThroughSeq, 0);
  assert.equal(Object.hasOwn(f.session.snapshotEvents()[0], 'ignorable'), false, 'recovery configuration must remain required');
  f.operations.length = 0;
  f.session.append('session/title', { title: 'audited tail' });
  await f.service.barrier(f.session.id);
  assert.deepEqual(f.operations, ['flush', ['open', 'source-session', 'read'], ['read', 1], 'close']);
  assert.equal(f.service.getMemory(f.session.id, 'event:source-session:1').content.data.title, 'audited tail');
});

test('failed read closes its handle and cannot acknowledge memory durability', async t => {
  const failure = new Error('fixture read failed');
  const f = fixture(t, { readError: failure });
  await assert.rejects(f.configure(), failure);
  assert.equal(f.operations.at(-1), 'close');
  assert.equal(f.service.status(f.session.id).durableThroughSeq, null);
  await assert.rejects(f.service.barrier(f.session.id), failure);
});

test('failed close cannot advance memory durability', async t => {
  const failure = new Error('fixture close failed');
  const f = fixture(t, { closeError: failure });
  await assert.rejects(f.configure(), failure);
  assert.equal(f.service.status(f.session.id).durableThroughSeq, null);
});

test('failed native flush never reads or advances the derived audit', async t => {
  const failure = new Error('fixture flush failed');
  const f = fixture(t, { flushError: failure });
  await assert.rejects(f.configure(), failure);
  assert.deepEqual(f.operations, ['flush']);
  assert.equal(f.service.status(f.session.id).durableThroughSeq, null);
});

test('native prefix loss or mutation fails closed after releasing the reader', async t => {
  for (const storedEvents of [() => [], events => events.map(event => ({ ...event, time: event.time + 1 }))]) {
    const f = fixture(t, { storedEvents });
    await assert.rejects(f.configure(), { code: 'MEMORY_SOURCE_CONFLICT' });
    assert.equal(f.operations.at(-1), 'close');
    assert.equal(f.service.status(f.session.id).durableThroughSeq, null);
  }
});

test('first memory context follows the native system head and later replaces only its own surface node', async t => {
  const f = fixture(t, { validateFormat: true }); await f.configure();
  f.session.append('turn/start', { turn: 1 });
  const user = { id: 'first-user', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Remember alpha' }] };
  const decision = await f.preStep([user]);
  assert.equal(f.session.surface.nodes.length, 0, 'pre-step must not occupy the system head');
  assert.equal(decision.messages.length, 2);
  const source = f.session.snapshotEvents().find(e => e.type === 'memory-recovery/page-selection');
  assert.equal(source.data.messageId, decision.messages[1].id);
  // Replay the real AgentLoop admission order through real native validators.
  f.session.append('step/start', { turn: 1, step: 1 });
  const head = f.session.append('system/message', { turn: 1, step: 1,
    message: { id: 'system-head', role: 'system', source: { kind: 'plugin', plugin: 'native' }, content: [] } }, { surfaceOp: 'append' });
  for (const message of decision.messages) f.session.append('user/message', message, { surfaceOp: 'append' });
  await f.service.barrier(f.session.id);
  const first = f.session.snapshotEvents().find(e => e.data.id === decision.messages[1].id);
  const derived = f.service.getMemory(f.session.id, `event:${f.session.id}:${first.seq}`);
  assert.deepEqual(derived.content.data.nativeSourceEventSeqs, [source.seq]);
  assert.equal(Object.hasOwn(derived.content.data, 'content'), false, 'derived bodies cannot feed paging');
  const secondUser = { ...user, id: 'second-user', content: [{ type: 'text', text: 'Remember beta' }] };
  const second = await f.preStep([secondUser]);
  assert.deepEqual(second.messages, [secondUser]);
  const replacements = f.session.snapshotEvents().filter(e => e.surfaceOp?.op === 'replace');
  assert.equal(replacements.length, 1);
  assert.deepEqual(replacements[0].surfaceOp, { op: 'replace', startSeq: first.seq, endSeq: first.seq });
  assert.ok(replacements[0].sourceEventSeqs.includes(first.seq));
  assert.equal(f.session.surface.nodes[0], head.seq);
  assert.equal(f.session.surface.nodes.includes(first.seq), false);
  assert.equal(f.session.surface.nodes.includes(replacements[0].seq), true);
  await f.service.barrier(f.session.id);
  // Tool-driven paging often enters a next step without a new inbox message.
  const operator = await f.service.putOperatorMemory(f.session.id, { kind: 'semantic', visibility: 'private',
    content: { nodes: [{ id: 'page-body', type: 'fact', label: 'UNIQUE_PAGE_BODY' }], edges: [] }, summary: 'Explicit tool-selected page' }, 'source regression');
  await f.service.pageIn(f.session.id, operator.id);
  assert.deepEqual(await f.preStep([]), { kind: 'enter', messages: [] });
  const latest = f.session.surface.nodes.map(seq => f.session.snapshotEvents()[seq]).find(e => e.type === 'user/message' && e.data.source.kind === memoryPlugin.name);
  assert.match(latest.data.content[0].text, /UNIQUE_PAGE_BODY/);
});

test('memory pre-step preserves native empty-input short circuit', async t => {
  const f = fixture(t, { validateFormat: true }); await f.configure();
  const seq = f.session.seq;
  assert.deepEqual(await f.preStep([]), { kind: 'enter', messages: [] });
  assert.equal(f.session.seq, seq);
});

test('official historical validator refuses legacy custom memory events instead of silently dropping them', () => {
  assert.throws(() => assertReleasedEventPayload({ type: 'memory-recovery/config', seq: 0, time: 1, data: {} }, 0), /unknown historical event/);
});


test('platform required-event registrations retain overlapping owners and preserve upstream entries', () => {
  const nativeType = KNOWN_SESSION_EVENT_TYPES.values().next().value;
  const firstType = 'plugin-isolation/first';
  const secondType = 'plugin-isolation/second';
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(firstType), false);
  const releaseFirst = retainRequiredEventTypes([nativeType, firstType, firstType]);
  const releaseSecond = retainRequiredEventTypes([firstType, secondType]);
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(firstType), true);
  releaseFirst(); releaseFirst();
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(nativeType), true);
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(firstType), true);
  releaseSecond();
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(firstType), false);
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(secondType), false);
  assert.equal(KNOWN_SESSION_EVENT_TYPES.has(nativeType), true);
});

test('platform event shim rejects unsupported catalogs and invalid types before changing the set', () => {
  const size = KNOWN_SESSION_EVENT_TYPES.size;
  assert.throws(() => retainRequiredEventTypes(['plugin-isolation/unused', '']), TypeError);
  assert.equal(KNOWN_SESSION_EVENT_TYPES.size, size);
  KNOWN_SESSION_EVENT_TYPES.has = value => Set.prototype.has.call(KNOWN_SESSION_EVENT_TYPES, value);
  try {
    assert.throws(() => retainRequiredEventTypes(['plugin-isolation/unused']), { code: 'MEMORY_NATIVE_EVENT_CATALOG_UNSUPPORTED' });
  } finally { delete KNOWN_SESSION_EVENT_TYPES.has; }
  assert.equal(KNOWN_SESSION_EVENT_TYPES.size, size);
});


test('platform session snapshot bridge refuses removed or malformed APIs instead of assuming empty history', () => {
  assert.throws(() => snapshotSessionEvents({ events: [] }), { code: 'MEMORY_NATIVE_SESSION_API_UNSUPPORTED' });
  assert.throws(() => snapshotSessionEvents({ snapshotEvents: () => undefined }), { code: 'MEMORY_NATIVE_SESSION_API_UNSUPPORTED' });
  const events = Object.freeze([{ seq: 2, type: 'session/title', data: { title: 'retained' } }]);
  assert.equal(snapshotSessionEvents({ snapshotEvents(from, to) { assert.equal(from, 2); assert.equal(to, 3); return events; } }, 2, 3), events);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import * as memoryPlugin from '../src/memory-recovery-plugin.mjs';
import { MemoryStore } from '../src/memory-store.mjs';
import { EffectStore } from '../src/effect-store.mjs';

const native = path => import(new URL(`../../../third_party/deepseek-harness/packages/${path}/lib/index.js`, import.meta.url));
const require = createRequire(new URL('../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url));
const { Context } = require('@deepseek-ai/cordis');
const { default: Sessions, KNOWN_SESSION_EVENT_TYPES } = await import('@deepseek-ai/dsh-session').catch(cause => {
  if (cause.code !== 'ERR_MODULE_NOT_FOUND') throw cause;
  return native('core/session');
});
const { default: Tools } = await native('core/tools');
const { default: Prompt } = await native('core/system-prompt');
const { default: Llm, createUserMessage } = await native('llm/llm');
const { default: Agents } = await native('core/agent');
const { default: Loop } = await native('core/agent-loop');
const { default: Sandbox } = await native('sandbox/sandbox-local');
const { default: Policy } = await native('sandbox/sandbox-policy');
const { default: JsonlPersistence } = await native('session/session-persistence-jsonl');

async function fixture(t, options = {}) {
  const root = mkdtempSync(join(homedir(), 'dsh-memory-runtime-'));
  const workspaceRoot = join(root, 'workspace'); mkdirSync(workspaceRoot);
  t.diagnostic(`Retained native integration fixture: ${root}`);
  const ctx = new Context();
  for (const plugin of [Llm, Sessions, Prompt]) await ctx.plugin(plugin);
  await ctx.plugin(Tools, options.toolsConfig ?? {});
  await ctx.plugin(Agents);
  await ctx.plugin(JsonlPersistence, { root: join(root, 'sessions'), compression: 'none', packChunks: false });
  await ctx.plugin(Loop, { agents: [] });
  await ctx.plugin(Sandbox);
  await ctx.plugin(Policy, { mode: options.nativeMode ?? 'workspace-write', workspaceRoot });
  options.seedDatabase?.(join(root, 'memory.sqlite'), workspaceRoot);
  await ctx.plugin(memoryPlugin, { path: join(root, 'memory.sqlite'), enabled: true });
  t.after(() => ctx.fiber.dispose());
  const agent = ctx.agentLoop.create('new-session', {}, { cwd: workspaceRoot });
  const service = ctx.memoryRecovery;
  const config = { enabled: true, projectId: 'project-a', agentId: agent.id, workspaceRoot,
    sandbox: { sandbox_required: true, mode: 'workspace-write', network: 'host', workspaceRoot },
    budgetTokens: 4000, anchors: ['Keep native tool pairs intact'] };
  const configure = () => service.configureSession(agent.id, config, { expectedVersion: 0 });
  const execute = (name, args, callId = `call-${agent.session.seq}`) => ctx.tools.execute({ agent, name, arguments: args, callId, signal: new AbortController().signal });
  return { root, workspaceRoot, ctx, agent, service, config, configure, execute };
}

// Real registry definitions, not a stand-in registry: a dispatch would return
// a detectable value if the plugin accidentally made an unsupported path callable.
function registerNativeTool(ctx, name) {
  return ctx.tools.register({ name, description: `${name} native capability`, parameters: { type: 'object', properties: {} },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    async execute() { return `${name} dispatched`; } });
}
const advertisedNames = async (ctx, agent) => (await ctx.systemPrompt.assemble({ scope: agent })).tools.map(tool => tool.name);

test('memory opt-in preserves native capabilities and hides memory tools until they are usable', async t => {
  const f = await fixture(t);
  for (const name of ['Bash', 'Glob', 'Read']) registerNativeTool(f.ctx, name);
  assert.deepEqual(await advertisedNames(f.ctx, f.agent), ['Bash', 'Glob', 'Read']);
  await f.service.configureSession(f.agent.id, { ...f.config, sandbox: null }, { expectedVersion: 0 });
  const enabled = await advertisedNames(f.ctx, f.agent);
  for (const name of ['Bash', 'Glob', 'Read', 'memory_search']) assert.ok(enabled.includes(name), name);
  assert.equal(enabled.includes('memory_sandbox_run'), false);
  assert.equal((await f.execute('Bash', {})).value, 'Bash dispatched');
  await f.service.configureSession(f.agent.id, { ...f.config, enabled: false, sandbox: null }, { expectedVersion: 1 });
  assert.deepEqual(await advertisedNames(f.ctx, f.agent), ['Bash', 'Glob', 'Read']);
});

test('required sandbox catalog is session-scoped and filters own scoped tools without weakening the guard', async t => {
  const f = await fixture(t);
  for (const name of ['Bash', 'Glob', 'Read']) registerNativeTool(f.ctx, name);
  registerNativeTool(f.agent.ctx, 'OwnUnconfined');
  const ordinary = f.ctx.agentLoop.create('ordinary', {}, { cwd: f.workspaceRoot });
  await f.configure();
  const names = await advertisedNames(f.ctx, f.agent);
  assert.ok(names.includes('memory_sandbox_run'));
  assert.equal(names.some(name => ['Bash', 'Glob', 'Read', 'OwnUnconfined', 'run_code'].includes(name)), false);
  assert.equal(f.ctx.tools.get('Bash', f.agent), undefined);
  assert.deepEqual(await advertisedNames(f.ctx, ordinary), ['Bash', 'Glob', 'Read']);
  // Own-scope tools are deliberately exempt from the native registry mask;
  // direct/stale calls must still be stopped by the unchanged execution guard.
  const denied = await f.execute('OwnUnconfined', {});
  assert.equal(denied.isError, true); assert.match(denied.error.message, /SANDBOX_REQUIREMENT_UNSUPPORTED/);
  registerNativeTool(f.ctx, 'LateNative');
  assert.equal((await advertisedNames(f.ctx, f.agent)).includes('LateNative'), false);
  assert.ok((await advertisedNames(f.ctx, ordinary)).includes('LateNative'));
});

test('required sandbox replaces deployment code transport with callable confined native tools', async t => {
  const f = await fixture(t, { toolsConfig: { mode: 'code' } });
  await f.configure();
  assert.equal(f.ctx.tools.get('run_code', f.agent), undefined);
  assert.ok((await advertisedNames(f.ctx, f.agent)).includes('memory_sandbox_run'));
  const result = await f.execute('memory_sandbox_run', { argv: ['pwd'] });
  assert.equal(result.isError, false, result.error?.message);
  assert.equal(result.value.stdout.trim(), f.workspaceRoot);
});

test('pre-step rejects incompatible late code presentation before advertising an unusable transport', async t => {
  const f = await fixture(t); await f.configure();
  f.agent.ctx.tools.presentAs('code');
  await assert.rejects(f.ctx.waterfall('agent/pre-step', { agent: f.agent, messages: [] },
    async () => ({ kind: 'enter', messages: [] })), { code: 'SANDBOX_REQUIREMENT_UNSUPPORTED' });
  assert.equal(f.service.getSessionConfig(f.agent.id).sandbox.sandbox_required, true);
  assert.equal(f.service.listEffects(f.agent.id).length, 0);
});

test('failed required-sandbox presentation preflight preserves the saved policy and tool scope', async t => {
  const f = await fixture(t);
  registerNativeTool(f.ctx, 'Bash');
  await f.service.configureSession(f.agent.id, { ...f.config, sandbox: null }, { expectedVersion: 0 });
  f.agent.ctx.tools.presentAs('code');
  const before = f.service.getSessionConfig(f.agent.id);
  const schemas = f.ctx.tools.schemas(f.agent).map(tool => tool.name);
  const seq = f.agent.session.seq;
  await assert.rejects(f.service.configureSession(f.agent.id, f.config, { expectedVersion: 1 }),
    { code: 'SANDBOX_REQUIREMENT_UNSUPPORTED' });
  assert.deepEqual(f.service.getSessionConfig(f.agent.id), before);
  assert.equal(f.agent.session.seq, seq);
  assert.deepEqual(f.ctx.tools.schemas(f.agent).map(tool => tool.name), schemas);
  assert.ok(f.ctx.tools.get('Bash', f.agent));
  assert.ok(f.ctx.tools.get('run_code', f.agent));
  assert.equal(f.ctx.tools.get('memory_sandbox_run', f.agent), undefined);
  await f.service.barrier(f.agent.id);
  assert.equal((await f.ctx.sessionPersistence.readFrom(f.agent.id, 0)).events.filter(event => event.type === 'memory-recovery/config').length, 1);
});

test('persisted required session reopens with a confined catalog before its first step', async t => {
  const f = await fixture(t); await f.configure();
  await f.ctx.sessions.flush(f.agent.session);
  await f.ctx.fiber.dispose();
  const ctx = new Context();
  for (const plugin of [Llm, Sessions, Prompt, Tools, Agents]) await ctx.plugin(plugin);
  await ctx.plugin(JsonlPersistence, { root: join(f.root, 'sessions'), compression: 'none', packChunks: false });
  await ctx.plugin(Loop, { agents: [] });
  await ctx.plugin(Sandbox); await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: f.workspaceRoot });
  await ctx.plugin(memoryPlugin, { path: join(f.root, 'memory.sqlite'), enabled: true });
  t.after(() => ctx.fiber.dispose());
  registerNativeTool(ctx, 'Bash');
  const resumed = await ctx.agents.resume({ resumeSessionId: f.agent.id });
  const agent = resumed.agent;
  const names = await advertisedNames(ctx, agent);
  assert.ok(names.includes('memory_sandbox_run')); assert.equal(names.includes('Bash'), false);
  await ctx.waterfall('agent/pre-step', { agent, messages: [] }, async () => ({ kind: 'enter', messages: [] }));
  assert.equal((await advertisedNames(ctx, agent)).includes('Bash'), false);
  await assert.rejects(ctx.memoryRecovery.configureSession(agent.id, { ...f.config, sandbox: null }, { expectedVersion: 1 }), { code: 'MEMORY_POLICY_DOWNGRADE_FORBIDDEN' });
});

test('explicit new-session opt-in persists config and flushes sourced events, old sessions stay disabled', async t => {
  const f = await fixture(t);
  assert.equal(f.service.getSessionConfig(f.agent.id).enabled, false);
  await f.configure();
  assert.equal(f.service.status(f.agent.id).executionCoverage, 'managed-sandbox-only');
  const user = f.agent.session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'remember project alpha' }] }), { surfaceOp: 'append' });
  await f.ctx.sessions.flush(f.agent.session);
  assert.equal(f.service.status(f.agent.id).durableThroughSeq, user.seq);
  assert.equal(f.service.listMemories(f.agent.id).some(r => r.content.data?.content?.[0]?.text === 'remember project alpha'), true);
  const old = f.ctx.sessions.create('old', { seed: f.agent.session.events, meta: { cwd: f.workspaceRoot } });
  assert.equal(f.service.getSessionConfig(old.id).enabled, false);
  await assert.rejects(f.service.configureSession(old.id, { ...f.config, agentId: old.id }, { expectedVersion: 0 }), /new|seed|scope/i);
  await assert.rejects(f.service.configureSession(f.agent.id, f.config, { expectedVersion: 0 }), /version/i);
});

test('fresh native permission initialization is allowed but seeded or started lifecycles are not', async t => {
  const f = await fixture(t);
  // Reproduce the native permission-presets publication ordering seen in real Web:
  // its earlier session/created observer appends these policy facts synchronously.
  f.ctx.on('session/created', session => {
    if (session.seq !== 0) return;
    session.append('permission/preset', { preset: 'workspace-write' });
    session.append('sandbox/mode', { mode: 'workspace-write' });
    session.append('approval/policy', { policy: 'ask' });
  }, { prepend: true });
  const fresh = f.ctx.sessions.create('initialized-new', { meta: { cwd: f.workspaceRoot } });
  assert.equal(f.service.getSessionConfig(fresh.id).canConfigure, true);
  assert.equal(f.service.getSessionConfig(fresh.id).configurationBlockReason, null);
  const initialEvents = fresh.events;
  assert.equal(fresh.firstLiveSeq, 0); assert.equal(fresh.seq, 3);
  const configured = await f.service.configureSession(fresh.id, { ...f.config, agentId: fresh.id }, { expectedVersion: 0 });
  assert.equal(configured.enabled, true);
  assert.equal(f.service.status(fresh.id).durableThroughSeq, 3);
  for (const [id, seed] of [['seeded-policy-only', initialEvents], ['seeded-empty', []]]) {
    const historical = f.ctx.sessions.create(id, { seed, meta: { cwd: f.workspaceRoot, parentSession: fresh.id, seedLength: seed.length } });
    assert.equal(f.service.getSessionConfig(id).canConfigure, false);
    assert.equal(f.service.getSessionConfig(id).configurationBlockReason, 'MEMORY_NEW_SESSION_REQUIRED');
    await assert.rejects(f.service.configureSession(id, { ...f.config, agentId: id }, { expectedVersion: 0 }), { code: 'MEMORY_NEW_SESSION_REQUIRED' });
    assert.equal(f.service.getSessionConfig(historical.id).enabled, false);
  }
  const started = f.ctx.sessions.create('already-started', { meta: { cwd: f.workspaceRoot } });
  started.append('turn/start', { turn: 1 });
  assert.equal(f.service.getSessionConfig(started.id).configurationBlockReason, 'MEMORY_NEW_SESSION_REQUIRED');
  await assert.rejects(f.service.configureSession(started.id, { ...f.config, agentId: started.id }, { expectedVersion: 0 }), { code: 'MEMORY_NEW_SESSION_REQUIRED' });
  const queued = f.ctx.sessions.create('message-before-turn', { meta: { cwd: f.workspaceRoot } });
  queued.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'queued user task' }] }), { surfaceOp: 'append' });
  assert.equal(f.service.getSessionConfig(queued.id).canConfigure, false, 'a queued message is not an untouched draft');
  await assert.rejects(f.service.configureSession(queued.id, { ...f.config, agentId: queued.id }, { expectedVersion: 0 }), { code: 'MEMORY_NEW_SESSION_REQUIRED' });
  const emptyRoot = f.ctx.sessions.create('empty-fork-source', { meta: { cwd: f.workspaceRoot } });
  const child = f.ctx.sessions.fork(emptyRoot, undefined, 'empty-real-fork');
  assert.equal(f.service.getSessionConfig(child.id).canConfigure, false, 'fork lineage is preserved even without turns');
});

test('native memory tools enforce scope and model cannot confirm procedural records', async t => {
  const f = await fixture(t); await f.configure();
  const record = await f.service.putOperatorMemory(f.agent.id, { kind: 'semantic', visibility: 'private', summary: 'alpha contract', content: { nodes: [{ id: 'alpha', type: 'contract' }], edges: [] } }, 'Initial contract');
  const found = await f.execute('memory_search', { query: 'alpha' });
  assert.equal(found.isError, false); assert.equal(found.value[0].id, record.id);
  assert.equal(Object.hasOwn(found.value[0], 'content'), false);
  const other = f.ctx.agentLoop.create('other', {}, { cwd: f.workspaceRoot });
  await f.service.configureSession(other.id, { ...f.config, projectId: 'project-b', agentId: other.id }, { expectedVersion: 0 });
  assert.equal(f.service.getMemory(other.id, record.id), null);
  const denied = await f.execute('memory_procedural_candidate', { summary: 'escape', content: { rules: ['escape'], applicability: {}, protected: true }, status: 'confirmed', sourceRefs: [{ type: 'operator', operatorId: 'operator', reason: 'fake' }] });
  assert.equal(denied.isError, true);
});

test('page selection changes logged native surface without duplicating pages or touching original messages', async t => {
  const f = await fixture(t); await f.configure();
  const record = await f.service.putOperatorMemory(f.agent.id, { kind: 'semantic', visibility: 'private', summary: 'contract', content: { nodes: [{ id: 'alpha', type: 'contract', detail: 'FULL PAGE BODY' }], edges: [] } }, 'Initial contract');
  const direct = createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'User authority stays' }] });
  f.agent.session.append('user/message', direct, { surfaceOp: 'append' });
  assert.equal((await f.execute('memory_page_in', { id: record.id })).isError, false);
  const payload = { agent: f.agent, messages: [], turn: 1, step: 1, signal: new AbortController().signal };
  await f.ctx.waterfall('agent/pre-step', payload, async () => ({ kind: 'enter', messages: [] }));
  const count = f.agent.session.seq;
  await f.ctx.waterfall('agent/pre-step', payload, async () => ({ kind: 'enter', messages: [] }));
  assert.equal(f.agent.session.seq, count);
  assert.match(JSON.stringify(f.agent.session.deriveMessages()), /FULL PAGE BODY/);
  await f.execute('memory_page_out', { id: record.id });
  await f.ctx.waterfall('agent/pre-step', payload, async () => ({ kind: 'enter', messages: [] }));
  assert.doesNotMatch(JSON.stringify(f.agent.session.deriveMessages()), /FULL PAGE BODY/);
  assert.equal(f.agent.session.deriveMessages().some(message => message.id === direct.id), true);
  assert.equal(f.agent.session.surface.replaceGeneration, 1);
  assert.ok(f.agent.session.events.at(-1).sourceEventSeqs.length > 0);
  assert.ok(f.service.getMemory(f.agent.id, record.id));
});

test('sandbox tool keeps native approval, durable effect ordering and strict native policy', async t => {
  const f = await fixture(t); await f.configure();
  const file = join(f.workspaceRoot, 'counter');
  const argv = [process.execPath, '-e', 'const fs=require("node:fs");const p=process.argv[1];fs.writeFileSync(p,String(Number(fs.existsSync(p)?fs.readFileSync(p):0)+1));', file];
  const deny = f.ctx.on('tools/pre-execute', async () => ({ kind: 'deny', reason: 'Native approval denied' }));
  assert.equal((await f.execute('memory_sandbox_run', { argv }, 'write-once')).isError, true);
  assert.equal(existsSync(file), false); deny();
  const result = await f.execute('memory_sandbox_run', { argv }, 'write-once');
  assert.equal(result.isError, false); assert.equal(result.value.state, 'COMMITTED');
  assert.equal(readFileSync(file, 'utf8'), '1');
  assert.equal((await f.execute('memory_sandbox_run', { argv }, 'write-once')).value.state, 'COMMITTED');
  assert.equal(readFileSync(file, 'utf8'), '1');
  assert.equal(f.service.listEffects(f.agent.id)[0].state, 'COMMITTED');
  const io = f.agent.session.events.find(event => event.type === 'memory-recovery/tool-io' && event.data.name === 'memory_sandbox_run');
  assert.deepEqual(io.data.input.argv, argv); assert.equal(io.data.value.backend, 'native-seatbelt');
});

test('required sandbox blocks unsupported tools, native read-only cannot be widened', async t => {
  const f = await fixture(t, { nativeMode: 'read-only' }); await f.configure();
  f.ctx.tools.register({ name: 'UnconfinedWrite', description: 'test actual unsupported native registry tool', parameters: { type: 'object', properties: {} }, output: { schema: { type: 'null' }, render: () => [] }, async execute() { throw new Error('MUST NOT DISPATCH'); } });
  const blocked = await f.execute('UnconfinedWrite', {});
  assert.equal(blocked.isError, true); assert.match(blocked.error.message, /SANDBOX.*UNSUPPORTED/);
  const result = await f.execute('memory_sandbox_run', { argv: [process.execPath, '-e', 'require("node:fs").writeFileSync("denied","no")'] });
  assert.equal(result.isError, false); assert.notEqual(result.value.exitCode, 0);
  assert.equal(existsSync(join(f.workspaceRoot, 'denied')), false);
  assert.equal(f.service.listEffects(f.agent.id)[0].sandboxPolicy.mode, 'read-only');
});

test('large canonical output is retained raw but native result uses a sourced summary handle', async t => {
  const f = await fixture(t);
  await f.service.configureSession(f.agent.id, { ...f.config, sandbox: null }, { expectedVersion: 0 });
  assert.equal(f.service.getSessionConfig(f.agent.id).executionCoverage, 'native-memory-only');
  const full = 'ORIGINAL LARGE BODY '.repeat(800);
  f.ctx.tools.register({ name: 'AuthorizedRead', description: 'canonical output fixture', parameters: { type: 'object', properties: {} }, output: { schema: { type: 'object' }, render: () => [{ type: 'text', text: full }] }, async execute() { return { full }; } });
  const call = f.agent.session.append('assistant/message', { turn: 1, step: 1, message: { id: 'assistant-call', role: 'assistant', source: { kind: 'model', provider: 'test-source', model: 'test-source' }, content: [{ type: 'tool-call', id: 'raw-call', name: 'AuthorizedRead', arguments: '{}' }] } }, { surfaceOp: 'append', sourceEventSeqs: [] });
  const result = await f.execute('AuthorizedRead', {}, 'raw-call');
  assert.equal(result.isError, false, result.error?.message);
  assert.deepEqual(result.value, { full });
  assert.ok(JSON.stringify(result.content).length < 1500);
  assert.match(result.content[0].text, /memory_page_in/);
  const io = f.agent.session.events.find(e => e.type === 'memory-recovery/tool-io');
  assert.equal(io.data.value.full, full); assert.deepEqual(io.data.sourceEventSeqs, [call.seq]);
  f.agent.session.append('tool/result', { turn: 1, step: 1, message: createUserMessage({ source: { kind: 'tool', callId: 'raw-call' }, content: [{ type: 'tool-result', toolCallId: 'raw-call', content: result.content }] }) }, { surfaceOp: 'append', sourceEventSeqs: [call.seq] });
  await f.service.barrier(f.agent.id);
  assert.doesNotMatch(JSON.stringify(f.agent.session.deriveMessages()), /ORIGINAL LARGE BODY/);
  // Consume exactly what the native result advertises, including the old prose form;
  // never reconstruct a corrected id from the separately inspected source event.
  const advertisedJson = result.content[0].text.match(/memory_page_in\((\{[^\n]+\})\)/)?.[1];
  const advertisedArgs = advertisedJson ? JSON.parse(advertisedJson)
    : { id: result.content[0].text.match(/memory handle (\S+)\. Use/)?.[1] };
  const page = await f.execute('memory_page_in', advertisedArgs);
  assert.equal(page.isError, false, page.error?.message);
  assert.equal(page.value.selected, true);
  await f.service.projectContext(f.agent.id);
  assert.match(JSON.stringify(f.agent.session.deriveMessages()), /ORIGINAL LARGE BODY/);
  await f.service.projectContext(f.agent.id);
  assert.match(JSON.stringify(f.agent.session.deriveMessages()), /ORIGINAL LARGE BODY/);
});

test('bounded observer rejects an oversized event and fails awaited flush and dispatch', async t => {
  const f = await fixture(t); await f.configure();
  f.agent.session.append('assistant/chunk', { text: 'x'.repeat(8 * 1024 * 1024) });
  assert.equal(f.service.status(f.agent.id).error, 'MEMORY_QUEUE_OVERFLOW');
  await assert.rejects(f.ctx.sessions.flush(f.agent.session), { code: 'MEMORY_QUEUE_OVERFLOW' });
  const result = await f.execute('memory_sandbox_run', { argv: ['true'] });
  assert.equal(result.isError, true); assert.equal(f.service.listEffects(f.agent.id).length, 0);
});

test('native append failure cannot advance projection and remains visible at every boundary', async t => {
  const f = await fixture(t, { persistence: true }); await f.configure();
  const persisted = f.ctx.sessionPersistence;
  const append = persisted.appendBatch;
  persisted.appendBatch = async () => { throw new Error('injected native disk failure'); };
  try {
    f.agent.session.append('assistant/chunk', { text: 'must not be projected' });
    await assert.rejects(f.ctx.sessions.flush(f.agent.session), /injected native disk failure/);
    assert.equal(f.service.status(f.agent.id).durableThroughSeq, 0);
    assert.equal(f.service.status(f.agent.id).error, 'MEMORY_DURABILITY_FAILED');
    assert.equal(f.service.getMemory(f.agent.id, `event:${f.agent.id}:1`), null);
    await assert.rejects(f.service.barrier(f.agent.id), /injected native disk failure/);
    const raw = await persisted.readFrom(f.agent.id, 0);
    assert.equal(raw.events.length, 1);
  } finally { persisted.appendBatch = append; }
});

test('long native streams drain without step boundaries and concurrent native flushes do not recurse', async t => {
  const f = await fixture(t, { persistence: true }); await f.configure();
  for (let i = 0; i < 1200; i++) {
    f.agent.session.append('assistant/chunk', { text: 'ordinary stream', i });
    if (i % 100 === 0) await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(f.service.status(f.agent.id).error, null);
  await Promise.all([f.ctx.sessions.flush(f.agent.session), f.service.barrier(f.agent.id), f.ctx.sessions.flush(f.agent.session)]);
  assert.equal(f.service.status(f.agent.id).pending, 0);
  assert.equal(f.service.status(f.agent.id).durableThroughSeq, 1200);
  assert.equal((await f.ctx.sessionPersistence.readFrom(f.agent.id, 0)).events.length, 1201);
  assert.equal(f.service.listMemories(f.agent.id, { limit: 1000 })[0].content.data.i, 1199);
});

test('cold recovery refuses a previously projected source that the native log does not contain', async t => {
  const f = await fixture(t, { persistence: true }); await f.configure();
  const external = new MemoryStore(join(f.root, 'memory.sqlite'));
  external.ingestEvents({ ...f.service.scopeForSession(f.agent.id), events: [{ seq: 1, type: 'assistant/chunk', time: 1, data: { text: 'legacy false durability' } }] });
  external.close();
  await f.ctx.fiber.dispose();
  const ctx = new Context();
  for (const plugin of [Sessions, Prompt, Tools]) await ctx.plugin(plugin);
  await ctx.plugin(JsonlPersistence, { root: join(f.root, 'sessions'), compression: 'none', packChunks: false });
  await ctx.plugin(Sandbox); await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: f.workspaceRoot });
  await ctx.plugin(memoryPlugin, { path: join(f.root, 'memory.sqlite'), enabled: true });
  t.after(() => ctx.fiber.dispose());
  const prepared = await ctx.sessionPersistence.prepare(f.agent.id);
  try {
    const restored = prepared.session;
    ctx.effect(() => ctx.sessions.enter(restored)); ctx.sessions.announce(restored);
    await assert.rejects(ctx.memoryRecovery.barrier(restored.id), { code: 'MEMORY_SOURCE_CONFLICT' });
    assert.equal(ctx.memoryRecovery.status(restored.id).error, 'MEMORY_SOURCE_CONFLICT');
    assert.equal(ctx.memoryRecovery.getMemory(restored.id, `event:${restored.id}:1`).content.data.text, 'legacy false durability');
  } finally { prepared[Symbol.dispose](); }
});

test('invalid requirements and running downgrade fail before dispatch without disabling isolation', async t => {
  const f = await fixture(t);
  await assert.rejects(f.service.configureSession(f.agent.id, { ...f.config, sandbox: { ...f.config.sandbox, network: 'none' } }, { expectedVersion: 0 }), { code: 'SANDBOX_REQUIREMENT_UNSUPPORTED' });
  await f.configure();
  await assert.rejects(f.service.configureSession(f.agent.id, { ...f.config, sandbox: null }, { expectedVersion: 1 }), { code: 'MEMORY_POLICY_DOWNGRADE_FORBIDDEN' });
  assert.equal(f.service.getSessionConfig(f.agent.id).version, 1);
  assert.equal(f.service.listEffects(f.agent.id).length, 0);
});

test('exclusive cold startup marks stranded execution unknown; live reads never recover active work', async t => {
  const f = await fixture(t, { seedDatabase(path, workspaceRoot) {
    const store = new MemoryStore(path); const effects = new EffectStore(store);
    const scope = { projectId: 'project-a', sessionId: 'new-session', agentId: 'new-session' };
    const prepared = effects.prepare({ ...scope, visibility: 'private', callId: 'stranded', stepId: 'step', toolName: 'memory_sandbox_run', input: {}, workspaceRoot, sandboxPolicy: {} });
    effects.begin(prepared.id, { id: 'dead-owner', scope }); store.close();
  } });
  await f.configure();
  assert.equal(f.service.listEffects(f.agent.id)[0].state, 'UNKNOWN');
  const running = f.execute('memory_sandbox_run', { argv: [process.execPath, '-e', 'setTimeout(()=>{},200)'] }, 'live');
  for (let i = 0; i < 50 && !f.service.listEffects(f.agent.id).some(e => e.callId === 'live'); i++) await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(f.service.listEffects(f.agent.id).find(e => e.callId === 'live').state, 'EXECUTING');
  f.service.listSessions(); f.service.effectGraph(f.agent.id);
  assert.equal(f.service.listEffects(f.agent.id).find(e => e.callId === 'live').state, 'EXECUTING');
  assert.equal((await running).value.state, 'COMMITTED');
});

test('interrupted native sandbox result remains unknown on repeated call and yields a sourced failure candidate', async t => {
  const f = await fixture(t);
  await f.service.configureSession(f.agent.id, { ...f.config, sandbox: { ...f.config.sandbox, timeoutMs: 150 } }, { expectedVersion: 0 });
  const argv = [process.execPath, '-e', 'const fs=require("node:fs");fs.appendFileSync("attempts","x");setInterval(()=>{},1000)'];
  const first = await f.execute('memory_sandbox_run', { argv }, 'unknown-call');
  assert.equal(first.value.state, 'UNKNOWN');
  const repeated = await f.execute('memory_sandbox_run', { argv }, 'unknown-call');
  assert.equal(repeated.value.state, 'UNKNOWN');
  assert.equal(readFileSync(join(f.workspaceRoot, 'attempts'), 'utf8'), 'x');
  const candidates = f.service.listMemories(f.agent.id, { kind: 'procedural' });
  assert.ok(candidates.length >= 1); assert.equal(candidates[0].status, 'candidate');
  assert.equal(candidates[0].sourceRefs[0].type, 'event');
});

test('restored native same-session config replays missing tail exactly and does not reingest derived bodies', async t => {
  const f = await fixture(t); await f.configure();
  await f.service.projectContext(f.agent.id);
  const last = f.agent.session.append('assistant/chunk', { text: 'unflushed-tail' });
  const seed = f.agent.session.events;
  await f.ctx.fiber.dispose();
  const ctx = new Context();
  for (const plugin of [Sessions, Prompt, Tools]) await ctx.plugin(plugin);
  await ctx.plugin(JsonlPersistence, { root: join(f.root, 'sessions'), compression: 'none', packChunks: false });
  await ctx.plugin(Sandbox); await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: f.workspaceRoot });
  await ctx.plugin(memoryPlugin, { path: join(f.root, 'memory.sqlite'), enabled: true });
  t.after(() => ctx.fiber.dispose());
  const restored = ctx.sessions.create('new-session', { seed, meta: { cwd: f.workspaceRoot } });
  await ctx.sessions.flush(restored);
  assert.equal(ctx.memoryRecovery.status(restored.id).durableThroughSeq, restored.seq - 1);
  assert.equal(ctx.memoryRecovery.getMemory(restored.id, `event:${restored.id}:${last.seq}`).content.data.text, 'unflushed-tail');
  const derived = ctx.memoryRecovery.listMemories(restored.id).find(r => r.content.data?.memoryRecoveryDerived);
  assert.equal(Object.hasOwn(derived.content.data, 'content'), false);
  await ctx.sessions.flush(restored);
});

test('native catalog registration is reference counted and preserves preexisting entries', async t => {
  const types = ['memory-recovery/config', 'memory-recovery/tool-io', 'memory-recovery/page-selection'];
  const catalog = KNOWN_SESSION_EVENT_TYPES;
  assert.equal(types.some(type => catalog.has(type)), false);
  catalog.add(types[0]); // Simulate a preexisting native registration, which we do not own.
  try {
    const first = await fixture(t);
    const second = await fixture(t);
    assert.equal(types.every(type => catalog.has(type)), true);
    await first.ctx.fiber.dispose();
    assert.equal(types.every(type => catalog.has(type)), true);
    await second.ctx.fiber.dispose();
    assert.equal(catalog.has(types[0]), true);
    assert.equal(catalog.has(types[1]), false);
    assert.equal(catalog.has(types[2]), false);
    assert.equal(catalog.has('user/message'), true);
  } finally { catalog.delete(types[0]); }
});

test('native catalog implementation mismatch fails plugin initialization explicitly', async t => {
  const f = await fixture(t);
  await f.ctx.fiber.dispose();
  const ctx = new Context();
  for (const plugin of [Sessions, Prompt, Tools, Sandbox]) await ctx.plugin(plugin);
  await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: f.workspaceRoot });
  t.after(() => ctx.fiber.dispose());
  KNOWN_SESSION_EVENT_TYPES.add = () => { throw new Error('must not call unknown implementation'); };
  try {
    await assert.rejects(async () => { await ctx.plugin(memoryPlugin, { path: join(f.root, 'memory.sqlite'), enabled: true }); }, { code: 'MEMORY_NATIVE_EVENT_CATALOG_UNSUPPORTED' });
  } finally { delete KNOWN_SESSION_EVENT_TYPES.add; }
});

test('real native persistence reopens required memory events without allowing unrelated unknown types', async t => {
  const f = await fixture(t, { persistence: true }); await f.configure();
  await f.service.projectContext(f.agent.id);
  assert.equal((await f.execute('memory_sandbox_run', { argv: [process.execPath, '-e', 'process.stdout.write("reopen-raw")'] }, 'persisted-call')).value.state, 'COMMITTED');
  await f.service.barrier(f.agent.id);
  const expectedTypes = ['memory-recovery/config', 'memory-recovery/page-selection', 'memory-recovery/tool-io'];
  const expected = f.agent.session.events.filter(event => expectedTypes.includes(event.type));
  assert.deepEqual(expected.map(event => event.type), expectedTypes);
  const unknown = f.ctx.sessions.create('unknown-required', { meta: { cwd: f.workspaceRoot } });
  unknown.append('memory-recovery/unregistered', { value: 'must reject' });
  await f.ctx.sessions.flush(unknown);
  await f.ctx.fiber.dispose();
  const ctx = new Context();
  for (const plugin of [Sessions, Prompt, Tools]) await ctx.plugin(plugin);
  await ctx.plugin(JsonlPersistence, { root: join(f.root, 'sessions'), compression: 'none', packChunks: false });
  await ctx.plugin(Sandbox); await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: f.workspaceRoot });
  await ctx.plugin(memoryPlugin, { path: join(f.root, 'memory.sqlite'), enabled: true });
  t.after(() => ctx.fiber.dispose());
  const prepared = await ctx.sessionPersistence.prepare(f.agent.id);
  try {
    const restored = prepared.session;
    assert.deepEqual(restored.events.filter(event => expectedTypes.includes(event.type)), expected);
    ctx.effect(() => ctx.sessions.enter(restored)); ctx.sessions.announce(restored);
    await ctx.sessions.flush(restored);
    assert.equal(ctx.memoryRecovery.getSessionConfig(restored.id).enabled, true);
    assert.equal(ctx.memoryRecovery.listEffects(restored.id)[0].state, 'COMMITTED');
    assert.equal(ctx.memoryRecovery.getMemory(restored.id, `event:${restored.id}:${expected[2].seq}`).content.data.value.stdout, 'reopen-raw');
  } finally { prepared[Symbol.dispose](); }
  await assert.rejects(ctx.sessionPersistence.prepare('unknown-required'), { name: 'SessionFormatUnsupportedError' });
});

test('trusted operator confirms a candidate but model operator impersonation and budget overflow fail closed', async t => {
  const f = await fixture(t); await f.configure();
  const source = f.agent.session.append('assistant/chunk', { text: 'source for reflection' });
  const record = { visibility: 'private', summary: 'candidate', content: { rules: ['Check returned result'], applicability: {} },
    sourceRefs: [{ type: 'event', projectId: 'project-a', sessionId: f.agent.id, agentId: f.agent.id, seq: source.seq }] };
  const candidate = await f.execute('memory_procedural_candidate', { record });
  assert.equal(candidate.isError, false); assert.equal(candidate.value.status, 'candidate');
  const forged = await f.execute('memory_procedural_candidate', { record: { ...record, sourceRefs: [{ type: 'operator', operatorId: 'local-operator', reason: 'spoof' }] } });
  assert.equal(forged.isError, true);
  const confirmed = await f.service.confirmProcedure(f.agent.id, candidate.value.id, { expectedVersion: 1, reason: 'Reviewed failure evidence' });
  assert.equal(confirmed.version, 2); assert.equal(confirmed.status, 'confirmed');
  assert.equal(confirmed.actor.role, 'operator');
  await f.service.configureSession(f.agent.id, { ...f.config, budgetTokens: 128, anchors: ['Invariant '.repeat(300)] }, { expectedVersion: 1 });
  await assert.rejects(f.service.projectContext(f.agent.id), { code: 'MEMORY_BUDGET_EXCEEDED' });
  assert.equal(f.agent.session.surface.nodes.length, 0);
});

test('old protected procedures survive unrelated records and mandatory overflow fails explicitly', async t => {
  const f = await fixture(t); await f.configure();
  const store = new MemoryStore(join(f.root, 'memory.sqlite'));
  const scope = f.service.scopeForSession(f.agent.id);
  const sourceRefs = [{ type: 'operator', operatorId: 'test-operator', reason: 'reviewed invariant' }];
  const put = (id, protectedRule, rule) => store.putMemory({ ...scope, id, kind: 'procedural', visibility: 'private', summary: id, status: 'confirmed',
    content: { protected: protectedRule, rules: [rule], applicability: {} }, sourceRefs }, { actor: { id: 'test-operator', role: 'operator' } });
  put('old-protected', true, 'MUST-RETAIN-OLD-INVARIANT');
  for (let i = 0; i < 1001; i++) put(`unrelated-${i}`, false, 'ordinary advice');
  const selection = await f.service.projectContext(f.agent.id);
  assert.match(selection.contextText, /MUST-RETAIN-OLD-INVARIANT/);
  put('over-budget-protected', true, 'mandatory '.repeat(2000));
  await assert.rejects(f.service.projectContext(f.agent.id), { code: 'MEMORY_BUDGET_EXCEEDED' });
  store.close();
});

test('native page-in tool selects the requested tail in the logged model surface', async t => {
  const f = await fixture(t); await f.configure();
  const record = await f.service.putOperatorMemory(f.agent.id, { kind: 'semantic', visibility: 'private', summary: 'Large contract',
    content: { nodes: [{ id: 'large', type: 'contract', text: 'x'.repeat(100100) + 'MODEL-TAIL-ONLY' }], edges: [] } }, 'Reviewed long source');
  const result = await f.execute('memory_page_in', { id: record.id, version: 1, offset: 100000, maxChars: 1000 });
  assert.equal(result.isError, false, result.error?.message);
  assert.equal(result.value.offset, 100000); assert.equal(result.value.hasMore, false);
  assert.equal(result.value.nextOffset, null);
  await f.service.projectContext(f.agent.id);
  assert.match(JSON.stringify(f.agent.session.deriveMessages()), /MODEL-TAIL-ONLY/);
  const logged = f.agent.session.events.find(e => e.type === 'memory-recovery/page-selection');
  assert.equal(logged.data.pages[0].offset, 100000);
  const seq = f.agent.session.seq;
  await f.service.projectContext(f.agent.id);
  assert.equal(f.agent.session.seq, seq);
});

test('database write failure remains sticky across native normalization and prevents later dispatch', async t => {
  const f = await fixture(t); await f.configure();
  const external = new MemoryStore(join(f.root, 'memory.sqlite'));
  external.db.exec("CREATE TRIGGER fail_runtime_ingest BEFORE INSERT ON memory_event_sources BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END");
  f.agent.session.append('assistant/chunk', { text: 'must reach durability' });
  await assert.rejects(f.ctx.sessions.flush(f.agent.session), /injected persistence failure/);
  const result = await f.execute('memory_sandbox_run', { argv: ['true'] });
  assert.equal(result.isError, true);
  assert.equal(f.service.listEffects(f.agent.id).length, 0);
  external.close();
});

test('second plugin instance cannot acquire the same live database or recover its active effects', async t => {
  const f = await fixture(t); await f.configure();
  const ctx = new Context();
  for (const plugin of [Sessions, Prompt, Tools]) await ctx.plugin(plugin);
  await ctx.plugin(Sandbox); await ctx.plugin(Policy, { mode: 'workspace-write', workspaceRoot: f.workspaceRoot });
  t.after(() => ctx.fiber.dispose());
  await assert.rejects(async () => { await ctx.plugin(memoryPlugin, { path: join(f.root, 'memory.sqlite'), enabled: true }); }, /already owned/);
  assert.equal((await f.execute('memory_search', { query: 'nothing' })).isError, false);
});

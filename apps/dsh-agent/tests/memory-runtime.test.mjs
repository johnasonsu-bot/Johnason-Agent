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
const { default: Sessions } = await native('core/session');
const { default: Tools } = await native('core/tools');
const { default: Prompt } = await native('core/system-prompt');
const { default: Llm, createUserMessage } = await native('llm/llm');
const { default: Agents } = await native('core/agent');
const { default: Loop } = await native('core/agent-loop');
const { default: Sandbox } = await native('sandbox/sandbox-local');
const { default: Policy } = await native('sandbox/sandbox-policy');

async function fixture(t, options = {}) {
  const root = mkdtempSync(join(homedir(), 'dsh-memory-runtime-'));
  const workspaceRoot = join(root, 'workspace'); mkdirSync(workspaceRoot);
  t.diagnostic(`Retained native integration fixture: ${root}`);
  const ctx = new Context();
  for (const plugin of [Llm, Sessions, Prompt, Tools, Agents]) await ctx.plugin(plugin);
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
  const page = f.service.pageIn(f.agent.id, `event:${f.agent.id}:${io.seq}`, { maxChars: 600 });
  assert.match(page.contentText, /ORIGINAL LARGE BODY/);
  await f.service.projectContext(f.agent.id);
  assert.match(JSON.stringify(f.agent.session.deriveMessages()), /ORIGINAL LARGE BODY/);
  await f.service.projectContext(f.agent.id);
  assert.match(JSON.stringify(f.agent.session.deriveMessages()), /ORIGINAL LARGE BODY/);
});

test('bounded observer overflow survives fire-and-forget and fails awaited flush and dispatch', async t => {
  const f = await fixture(t); await f.configure();
  for (let i = 0; i < 513; i++) f.agent.session.append('assistant/chunk', { text: 'stream', i });
  assert.equal(f.service.status(f.agent.id).error, 'MEMORY_QUEUE_OVERFLOW');
  await assert.rejects(f.ctx.sessions.flush(f.agent.session), /overflow/);
  const result = await f.execute('memory_sandbox_run', { argv: ['true'] });
  assert.equal(result.isError, true); assert.equal(f.service.listEffects(f.agent.id).length, 0);
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

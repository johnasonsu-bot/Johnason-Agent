import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';
import { realpathSync } from 'node:fs';
import { MemoryStore } from './memory-store.mjs';
import { MemoryPager } from './memory-pager.mjs';
import { EffectStore } from './effect-store.mjs';
import { sanitizeEvent } from './record-sanitizer.mjs';
import { validateSandboxRequirements, runSandboxTask } from './task-sandbox.mjs';
import { installMemoryTools, MEMORY_TOOL_NAMES } from './memory-tools.mjs';
import { installMemoryRecoveryUi } from './memory-recovery-ui.mjs';

const require = createRequire(new URL('../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url));
const { Service } = await import(require.resolve('@deepseek-ai/cordis'));
const { createUserMessage } = await import(new URL('../../../third_party/deepseek-harness/packages/llm/llm/lib/index.js', import.meta.url));
// Match the reader's ESM resolution: the native Web launcher uses tsx source aliases.
// Plain Node component hosts use the built package from the CLI dependency graph.
const { KNOWN_SESSION_EVENT_TYPES } = await import('@deepseek-ai/dsh-session').catch(cause => {
  if (cause.code !== 'ERR_MODULE_NOT_FOUND' || !cause.message.includes("package '@deepseek-ai/dsh-session'")) throw cause;
  return import(require.resolve('@deepseek-ai/dsh-session'));
});
export const name = 'johnason-memory-recovery';
export const inject = ['sessions', 'tools', 'sandbox', 'sandboxPolicy'];
const QUEUE_EVENTS = 512;
const QUEUE_BYTES = 8 * 1024 * 1024;
const CONFIG_EVENT = 'memory-recovery/config';
const IO_EVENT = 'memory-recovery/tool-io';
const INITIAL_POLICY_EVENTS = new Set(['permission/preset', 'sandbox/mode', 'approval/policy']);
const actor = Object.freeze({ id: 'local-operator', role: 'operator' });
const error = (code, message = code) => Object.assign(new Error(message), { code });
const owned = event => event.type === 'user/message' && event.data.source?.kind === 'plugin' && event.data.source.plugin === name;
const textOf = message => message.content?.filter(b => b.type === 'text').map(b => b.text).join('\n') ?? '';
const clean = data => sanitizeEvent({ seq: 0, type: IO_EVENT, time: Date.now(), data }).event.data;

// Compatibility shim for the pinned native reader, not a stable registration API.
// Replace with upstream's formal event registry when that extension seam exists.
const MEMORY_EVENT_TYPES = [CONFIG_EVENT, IO_EVENT, 'memory-recovery/page-selection'];
let catalogUsers = 0;
let catalogAdded = [];
function retainNativeEventCatalog() {
  const catalog = KNOWN_SESSION_EVENT_TYPES;
  if (!(catalog instanceof Set) || Object.getPrototypeOf(catalog) !== Set.prototype
      || ['has', 'add', 'delete'].some(method => catalog[method] !== Set.prototype[method])) {
    throw error('MEMORY_NATIVE_EVENT_CATALOG_UNSUPPORTED', 'Pinned native event catalog is no longer the supported Set implementation');
  }
  if (catalogUsers++ === 0) {
    catalogAdded = MEMORY_EVENT_TYPES.filter(type => !catalog.has(type));
    for (const type of catalogAdded) catalog.add(type);
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--catalogUsers === 0) {
      for (const type of catalogAdded) catalog.delete(type);
      catalogAdded = [];
    }
  };
}

// Database lock is not a renewable lease. Never steal from a live PID; ambiguous liveness
// fails closed. The row remains on disk (no lock-file deletion or fixture cleanup).
function acquireRuntime(db, token) {
  db.exec('CREATE TABLE IF NOT EXISTS memory_runtime_owner (id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER, token TEXT)');
  db.exec('BEGIN IMMEDIATE');
  try {
    const previous = db.prepare('SELECT pid FROM memory_runtime_owner WHERE id=1').get();
    if (previous?.pid) {
      let alive = true;
      try { process.kill(previous.pid, 0); } catch (cause) { if (cause.code === 'ESRCH') alive = false; }
      if (alive) throw error('MEMORY_RUNTIME_ALREADY_OWNED', 'memory database is already owned by a live process');
    }
    db.prepare('INSERT INTO memory_runtime_owner VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, token=excluded.token').run(process.pid, token);
    db.exec('COMMIT');
  } catch (cause) { db.exec('ROLLBACK'); throw cause; }
}

export function apply(ctx, config) {
  if (!config?.path || typeof config.enabled !== 'boolean') throw error('MEMORY_INVALID_CONFIG');
  return new MemoryRecovery(ctx, config);
}

/** All trusted roles and scopes are constructed here; HTTP/model fields never become actors. */
class MemoryRecovery extends Service {
  #store; #pager; #effects; #states = new Map(); #fresh = new WeakSet(); #enabled; #rawByToken = new Map(); #inflight = new Set(); #owner = randomUUID();
  constructor(ctx, config) {
    super(ctx, 'memoryRecovery');
    // Cordis service access uses a scoped proxy; preserve the private-field receiver.
    for (const key of Object.getOwnPropertyNames(MemoryRecovery.prototype)) {
      if (key !== 'constructor') this[key] = this[key].bind(this);
    }
    this.#enabled = config.enabled;
    this.#store = new MemoryStore(config.path);
    this.#effects = new EffectStore(this.#store);
    let releaseCatalog;
    try {
      acquireRuntime(this.#store.db, this.#owner);
      // The only recovery site: exclusive process startup, before any listener/tool dispatch.
      for (const scope of this.#store.db.prepare('SELECT DISTINCT project_id AS projectId, session_id AS sessionId, agent_id AS agentId FROM effect_identities').all()) this.#effects.recover(scope);
      releaseCatalog = retainNativeEventCatalog();
    } catch (cause) {
      this.#store.db.prepare('UPDATE memory_runtime_owner SET pid=NULL, token=NULL WHERE id=1 AND token=?').run(this.#owner);
      this.#store.close(); throw cause;
    }
    // Registered before observers, so their disposal completes before the final release.
    ctx.effect(() => releaseCatalog);
    // Derived context is retained in native history, but cannot feed itself back into paging.
    const visible = record => record && !record.content?.data?.memoryRecoveryDerived;
    this.#pager = new MemoryPager({ db: this.#store.db,
      list: (scope, filters) => this.#store.list(scope, filters).filter(visible),
      get: (scope, id, version) => { const record = this.#store.get(scope, id, version); return visible(record) ? record : null; },
    });
    ctx.on('session/created', session => {
      // Earlier native permission-preset observers may already have pinned policy.
      // firstLiveSeq is the public construction boundary, unlike the growing seq.
      if (!session.header.seedLength && session.firstLiveSeq === 0
          && session.events.every(event => INITIAL_POLICY_EVENTS.has(event.type))) this.#fresh.add(session);
    });
    ctx.on('session/event', (session, event) => {
      try {
        if (!this.getSessionConfig(session.id).enabled) return;
        this.#enqueue(session, event);
      } catch (cause) { this.#state(session.id).fault ??= cause; }
    });
    ctx.on('session/flush', session => this.flush(session.id));
    ctx.on('tools/pre-execute', async (exec, next) => {
      if (exec.agent && this.getSessionConfig(exec.agent.id).enabled) await this.barrier(exec.agent.id);
      return next();
    }, { prepend: true });
    // Monotonic guard cannot be bypassed by a later policy returning allow.
    ctx.tools.guard(exec => {
      if (!exec.agent || !this.getSessionConfig(exec.agent.id).enabled) return;
      const state = this.#state(exec.agent.id);
      if (state.fault) return `${state.fault.code ?? 'MEMORY_DURABILITY_FAILED'}: memory boundary failed`;
      if (this.getSessionConfig(exec.agent.id).sandbox?.sandbox_required && !MEMORY_TOOL_NAMES.has(exec.name)) {
        return 'SANDBOX_REQUIREMENT_UNSUPPORTED: use memory_sandbox_run; this tool is not a verified confined path';
      }
    });
    ctx.on('tools/execute', async (exec, next) => {
      if (!exec.agent || !this.getSessionConfig(exec.agent.id).enabled) return next();
      const id = exec.agent.id; const state = this.#state(id);
      state.active++;
      const settlement = Promise.withResolvers(); this.#inflight.add(settlement.promise);
      try {
        await this.barrier(id);
        const result = await next();
        if ((!exec.name.startsWith('memory_') || exec.name === 'memory_sandbox_run') && !/vault|credential|password|secret/i.test(exec.name)) {
          const session = exec.agent.session;
          const sources = session.events.filter(event => event.type === 'assistant/message'
            && event.data.message.content.some(block => block.type === 'tool-call' && (block.id === exec.rootCallId || block.id === exec.callId))).map(event => event.seq);
          const raw = session.append(IO_EVENT, clean({ name: exec.name, callId: exec.callId, rootCallId: exec.rootCallId,
            input: exec.arguments, ...(result.isError ? { error: result.error } : { value: result.value }),
            sourceEventSeqs: sources }));
          this.#rawByToken.set(exec.token, raw.seq);
          await this.barrier(id);
          if (result.isError || result.value?.state === 'UNKNOWN' || (typeof result.value?.exitCode === 'number' && result.value.exitCode !== 0)) {
            const scope = this.scopeForSession(id);
            this.#store.putMemory({ ...scope, kind: 'procedural', visibility: 'private', status: 'candidate',
              summary: `Review failed ${exec.name} invocation before repeating`,
              content: { rules: ['Inspect the sourced execution outcome and reconcile unknown side effects before any new retry.'], applicability: { toolName: exec.name, callId: exec.callId } },
              sourceRefs: [{ type: 'event', ...scope, seq: raw.seq }] }, { actor: { id: name, role: 'system' } });
          }
        }
        return result;
      } catch (cause) {
        // Native tool pipelines normalize exceptions; the next awaited boundary must still fail.
        if (!String(cause.code ?? '').startsWith('SANDBOX_')) state.fault ??= cause;
        throw cause;
      } finally { state.active--; settlement.resolve(); this.#inflight.delete(settlement.promise); }
    }, { prepend: true });
    ctx.on('tools/post-execute', async (exec, result, next) => {
      const decision = await next();
      if (!exec.agent || !this.getSessionConfig(exec.agent.id).enabled || decision.kind === 'block') return decision;
      const rawSeq = this.#rawByToken.get(exec.token);
      if (rawSeq === undefined) return decision;
      // Preserve execution-local canonical values. Only the logged/model projection is reduced.
      if (Object.hasOwn(decision, 'value')) {
        return { kind: 'block', feedback: [{ type: 'text', text: 'MEMORY_PROJECTION_UNSUPPORTED: downstream canonical value replacement requires a supported projection adapter' }] };
      }
      const content = decision.content ?? result.content;
      if (JSON.stringify(content).length <= 2048) return decision;
      const pageArguments = JSON.stringify({ id: `event:${exec.agent.id}:${rawSeq}`, version: 1 });
      return { ...decision, content: [{ type: 'text', text: `Large ${exec.name} result stored in memory. Use memory_page_in(${pageArguments}) for a bounded original page. Source: ${exec.agent.id}/${rawSeq}.` }] };
    }, { prepend: true });
    ctx.on('tools/result', exec => { this.#rawByToken.delete(exec.token); });
    ctx.on('agent/pre-step', async (payload, next) => {
      if (!this.getSessionConfig(payload.agent.id).enabled) return next();
      await this.barrier(payload.agent.id);
      const decision = await next();
      if (decision.kind === 'reject') return decision;
      await this.projectContext(payload.agent.id, decision.messages);
      return decision;
    }, { prepend: true });
    installMemoryTools(ctx, this);
    ctx.inject(['webServer', 'apiProxy'], ui => installMemoryRecoveryUi(ui, this));
    ctx.effect(() => async () => {
      await Promise.all(this.#inflight);
      try {
        for (const session of ctx.sessions.list()) if (this.getSessionConfig(session.id).enabled) this.flush(session.id);
      } finally {
        this.#store.db.prepare('UPDATE memory_runtime_owner SET pid=NULL, token=NULL WHERE id=1 AND token=?').run(this.#owner);
        this.#store.close();
      }
    });
  }

  #session(id) { const session = this.ctx.sessions.get(id); if (!session) throw error('MEMORY_SESSION_NOT_FOUND'); return session; }
  #state(id) {
    if (!this.#states.has(id)) this.#states.set(id, { queue: [], bytes: 0, fault: null, active: 0 });
    return this.#states.get(id);
  }
  getSessionConfig(id) {
    const session = this.#session(id);
    const found = [...session.events].reverse().find(event => event.type === CONFIG_EVENT && event.data.sessionId === id);
    const config = found ? { ...structuredClone(found.data), enabled: this.#enabled && found.data.enabled } : { sessionId: id, version: 0, enabled: false };
    return { ...config, executionCoverage: !config.enabled ? 'disabled' : config.sandbox ? 'managed-sandbox-only' : 'native-memory-only',
      executionNotice: !config.enabled ? 'Native session; memory/recovery disabled.' : config.sandbox
        ? 'Only memory_sandbox_run has Effect replay protection; other unverified native tools are blocked.'
        : 'Memory-only mode: native tools have NO Effect replay protection. Use a required sandbox task for managed execution.' };
  }
  listSessions() { return this.ctx.sessions.list().map(session => ({ id: session.id, cwd: session.header.cwd ?? null, config: this.getSessionConfig(session.id), status: this.status(session.id) })); }
  scopeForSession(id) {
    const config = this.getSessionConfig(id);
    if (!config.enabled) throw error('MEMORY_SESSION_DISABLED');
    return { projectId: config.projectId, sessionId: id, agentId: config.agentId };
  }
  status(id) {
    const state = this.#state(id);
    const config = this.getSessionConfig(id);
    return { enabled: config.enabled, executionCoverage: config.executionCoverage, executionNotice: config.executionNotice, pending: state.queue.length, pendingBytes: state.bytes,
      durableThroughSeq: this.#store.cursor(id), error: state.fault?.code ?? (state.fault ? 'MEMORY_DURABILITY_FAILED' : null), active: state.active };
  }
  async configureSession(id, input, { expectedVersion } = {}) {
    const session = this.#session(id); const previous = this.getSessionConfig(id); const state = this.#state(id);
    if (expectedVersion !== previous.version) throw error('MEMORY_CONFIG_VERSION_CONFLICT', 'session configuration version conflict');
    if (!previous.version && (!this.#fresh.has(session) || session.events.some(e => ['turn/start', 'user/message'].includes(e.type)))) throw error('MEMORY_NEW_SESSION_REQUIRED', 'only an explicit new unstarted session can enable memory');
    if (state.active || this.ctx.get('agents')?.get(id)?.status === 'running') throw error('MEMORY_SESSION_BUSY');
    if (!input || Object.keys(input).some(key => !['enabled', 'projectId', 'agentId', 'workspaceRoot', 'sandbox', 'budgetTokens', 'anchors'].includes(key))) throw error('MEMORY_INVALID_CONFIG');
    if (typeof input.enabled !== 'boolean' || typeof input.projectId !== 'string' || !input.projectId.trim()
        || input.agentId !== id || typeof input.workspaceRoot !== 'string') throw error('MEMORY_INVALID_CONFIG');
    const workspaceRoot = realpathSync.native(input.workspaceRoot);
    if (!session.header.cwd || realpathSync.native(session.header.cwd) !== workspaceRoot) throw error('MEMORY_WORKSPACE_MISMATCH');
    if (previous.version && (input.projectId !== previous.projectId || input.agentId !== previous.agentId || workspaceRoot !== previous.workspaceRoot)) throw error('MEMORY_SCOPE_IMMUTABLE');
    const sandbox = input.sandbox == null ? null : validateSandboxRequirements(input.sandbox);
    if (sandbox && sandbox.workspaceRoot !== workspaceRoot) throw error('MEMORY_WORKSPACE_MISMATCH');
    // Config changes cannot remove already-required isolation. A new session is required for that.
    if (previous.sandbox && (!sandbox || (previous.sandbox.mode === 'read-only' && sandbox.mode !== 'read-only'))) throw error('MEMORY_POLICY_DOWNGRADE_FORBIDDEN');
    if (previous.sandbox && !input.enabled) throw error('MEMORY_POLICY_DOWNGRADE_FORBIDDEN');
    const budgetTokens = input.budgetTokens ?? 4000; const anchors = input.anchors ?? [];
    if (!Number.isSafeInteger(budgetTokens) || budgetTokens < 128 || budgetTokens > 100000 || !Array.isArray(anchors) || anchors.some(a => typeof a !== 'string' || !a)) throw error('MEMORY_INVALID_CONFIG');
    const config = { sessionId: id, version: previous.version + 1, enabled: input.enabled,
      projectId: input.projectId, agentId: id, workspaceRoot, sandbox, budgetTokens, anchors };
    session.append(CONFIG_EVENT, config);
    if (config.enabled) {
      const through = this.#store.cursor(id) ?? -1;
      for (const event of session.events) if (event.seq > through && !state.queue.some(q => q.seq === event.seq)) this.#enqueue(session, event);
      await this.barrier(id);
    } else await this.ctx.sessions.flush(session);
    return this.getSessionConfig(id);
  }
  #enqueue(session, event) {
    const state = this.#state(session.id); if (state.fault) return;
    const bytes = Buffer.byteLength(JSON.stringify(event));
    if (state.queue.length >= QUEUE_EVENTS || state.bytes + bytes > QUEUE_BYTES) {
      state.fault = error('MEMORY_QUEUE_OVERFLOW', 'bounded memory event queue overflow; restart/replay required'); return;
    }
    state.queue.push(event); state.bytes += bytes;
  }
  flush(id) {
    const config = this.getSessionConfig(id); if (!config.enabled) return;
    const state = this.#state(id); if (state.fault) throw state.fault;
    try {
      const sourceEvents = this.#session(id).events;
      for (let start = (this.#store.cursor(id) ?? -1) + 1; start < sourceEvents.length; start += 64) {
        const events = sourceEvents.slice(start, start + 64).map(event => {
        if (owned(event)) {
          const { sourceEventSeqs, ...envelope } = event;
          return { ...envelope, data: { memoryRecoveryDerived: true, projectionVersion: 1,
            messageId: event.data.id, source: event.data.source, nativeSourceEventSeqs: sourceEventSeqs ?? [] } };
        }
        // Plugin log-only events cannot carry native surface metadata. The domain projection
        // promotes the exact logged earlier references for episodic tool-pair closure.
        if (event.type === IO_EVENT) return { ...event, sourceEventSeqs: event.data.sourceEventSeqs };
        return event;
        });
        this.#store.ingestEvents({ ...this.scopeForSession(id), events });
      }
      state.queue = []; state.bytes = 0;
    } catch (cause) { state.fault = cause; throw cause; }
  }
  async barrier(id) { this.flush(id); await this.ctx.sessions.flush(this.#session(id)); }
  listMemories(id, filters) { return this.#store.list(this.scopeForSession(id), filters); }
  getMemory(id, memoryId, version) { return this.#store.get(this.scopeForSession(id), memoryId, version); }
  semanticGraph(id, filters) { return this.#store.graph(this.scopeForSession(id), filters); }
  listEffects(id, filters) { return this.#effects.list(this.scopeForSession(id), filters); }
  effectGraph(id, filters) { return this.#effects.graph(this.scopeForSession(id), filters); }
  search(id, query, options) { this.flush(id); return this.#pager.search(this.scopeForSession(id), query, options); }
  pageIn(id, memoryId, options) { this.flush(id); return this.#pager.pageIn(this.scopeForSession(id), memoryId, options); }
  pageOut(id, memoryId) { return this.#pager.pageOut(this.scopeForSession(id), memoryId); }
  async putOperatorMemory(id, record, reason) {
    if (typeof reason !== 'string' || !reason.trim()) throw error('MEMORY_OPERATOR_REASON_REQUIRED');
    await this.barrier(id);
    const allowed = this.#memoryFields(record);
    return this.#store.putMemory({ ...allowed, ...this.scopeForSession(id), sourceRefs: [{ type: 'operator', operatorId: actor.id, reason }] }, { actor });
  }
  async confirmProcedure(id, memoryId, { expectedVersion, reason } = {}) {
    const current = this.getMemory(id, memoryId);
    if (!current || current.kind !== 'procedural') throw error('MEMORY_NOT_FOUND');
    if (expectedVersion !== current.version) throw error('MEMORY_VERSION_CONFLICT');
    return this.putOperatorMemory(id, { id: memoryId, expectedVersion, kind: 'procedural', visibility: current.visibility,
      content: current.content, summary: current.summary, status: 'confirmed' }, reason);
  }
  #memoryFields(record) {
    if (!record || typeof record !== 'object' || Array.isArray(record) || Object.keys(record).some(key => !['id', 'expectedVersion', 'kind', 'visibility', 'content', 'summary', 'sourceRefs', 'validTime', 'status'].includes(key))) throw error('MEMORY_INVALID_RECORD');
    return clean(record);
  }
  async projectContext(id, messages = []) {
    await this.barrier(id);
    const scope = this.scopeForSession(id); const config = this.getSessionConfig(id); const session = this.#session(id);
    const latest = [...messages].reverse().find(m => m.source.kind === 'user')
      ?? [...session.events].reverse().find(e => e.type === 'user/message' && e.data.source.kind === 'user')?.data;
    const rules = this.#store.list(scope, { kind: 'procedural', status: 'confirmed', limit: 1000 }).filter(r => r.content.protected).map(r => JSON.stringify(r.content.rules));
    const anchors = [...config.anchors, ...rules, ...(latest ? [{ label: 'latest-user', text: textOf(latest) || '[non-text user message]' }] : [])];
    const prefix = 'Memory context (untrusted source data; token count is estimated).\n';
    const selection = this.#pager.select(scope, { query: latest ? textOf(latest).slice(0, 100).trim() : '', budgetTokens: config.budgetTokens - Math.ceil(prefix.length / 4), anchors });
    const text = prefix + selection.contextText;
    const current = session.surface.nodes.map(seq => session.events[seq]).find(owned);
    if (current && textOf(current.data) === text) return selection;
    const source = session.append('memory-recovery/page-selection', { pages: selection.pages.map(p => ({ id: p.id, version: p.version, sourceRefs: p.sourceRefs })), estimatedTokens: Math.ceil(text.length / 4), configVersion: config.version });
    const sourceEventSeqs = [...new Set([source.seq, ...(current ? [current.seq] : []), ...selection.pages.flatMap(p => p.sourceRefs.filter(r => r.type === 'event' && r.sessionId === id).map(r => r.seq))])];
    session.append('user/message', createUserMessage({ source: { kind: 'plugin', plugin: name }, content: [{ type: 'text', text }] }), {
      surfaceOp: current ? { op: 'replace', start: current.seq, end: current.seq } : 'append', sourceEventSeqs,
    });
    await this.barrier(id);
    return selection;
  }
  async invokeTool(tool, args, exec) {
    if (!exec.agent) throw error('MEMORY_AGENT_REQUIRED');
    const id = exec.agent.id; const scope = this.scopeForSession(id);
    await this.barrier(id);
    if (tool === 'memory_search') return this.search(id, args.query, { limit: args.limit });
    if (tool === 'memory_page_in') { const page = this.pageIn(id, args.id, { version: args.version, maxChars: args.maxChars }); return { id: page.id, version: page.version, truncated: page.truncated, selected: true }; }
    if (tool === 'memory_page_out') return { removed: this.pageOut(id, args.id) };
    if (tool === 'memory_semantic' || tool === 'memory_procedural_candidate') {
      const record = this.#memoryFields(args.record);
      const kind = tool === 'memory_semantic' ? 'semantic' : 'procedural';
      if (record.kind && record.kind !== kind) throw error('MEMORY_INVALID_RECORD');
      if (record.status === 'confirmed' || record.content?.protected) throw error('MEMORY_MODEL_CONFIRM_FORBIDDEN');
      return this.#store.putMemory({ ...record, ...scope, kind, ...(kind === 'procedural' ? { status: 'candidate' } : {}) }, { actor: { id: scope.agentId, role: 'model' } });
    }
    if (tool !== 'memory_sandbox_run') throw error('MEMORY_UNKNOWN_TOOL');
    const config = this.getSessionConfig(id);
    if (!config.sandbox) throw error('SANDBOX_REQUIREMENT_UNSUPPORTED', 'explicit task sandbox requirements are required');
    const native = this.ctx.sandboxPolicy.resolve({ session: exec.agent.session });
    if (realpathSync.native(native.workspaceRoot) !== config.workspaceRoot) throw error('SANDBOX_REQUIREMENT_UNSUPPORTED', 'native workspace policy differs');
    const requirements = validateSandboxRequirements({ ...config.sandbox, mode: native.mode === 'read-only' ? 'read-only' : config.sandbox.mode });
    if (!Array.isArray(args.argv) || !args.argv.length || args.argv.some(a => typeof a !== 'string' || a.includes('\0'))) throw error('SANDBOX_INVALID_ARGV');
    const sanitized = clean(args);
    if (JSON.stringify(sanitized) !== JSON.stringify(args)) throw error('MEMORY_SENSITIVE_EXECUTION_INPUT', 'recognized secrets cannot enter the effect ledger');
    const step = [...exec.agent.session.events].reverse().find(e => e.type === 'step/start');
    const effect = this.#effects.prepare({ ...scope, visibility: 'private', callId: exec.callId,
      stepId: step ? `${step.data.turn}:${step.data.step}` : 'native-tool-dispatch', toolName: tool,
      input: sanitized, workspaceRoot: config.workspaceRoot, sandboxPolicy: { ...requirements, configVersion: config.version, nativeMode: native.mode } });
    if (effect.state !== 'PREPARED') return effect.result ?? { effectId: effect.id, state: effect.state, repeated: true };
    const owner = { id: randomUUID(), scope };
    this.#effects.begin(effect.id, owner);
    try {
      const result = await runSandboxTask({ sandbox: this.ctx.sandbox, argv: args.argv, requirements, signal: exec.signal });
      this.#effects.finish(effect.id, owner, { state: result.state, result: clean(result), evidence: { receipt: `native-process:${effect.id}` } });
      return result;
    } catch (cause) {
      this.#effects.finish(effect.id, owner, { state: 'UNKNOWN', result: { error: cause.code ?? 'SANDBOX_EXECUTION_FAILED' }, evidence: null });
      throw cause;
    }
  }
}

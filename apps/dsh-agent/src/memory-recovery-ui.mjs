import { readFile, realpath, stat } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { isAbsolute } from 'node:path';

const page = new URL('../public/memory-recovery.html', import.meta.url);
const local = address => ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address);
const invalid = () => { throw Object.assign(new Error('Invalid request'), { code: 'MEMORY_INVALID_REQUEST' }); };
function fields(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(k => !allowed.includes(k))) invalid();
}
function summaryHandle({ id, version, kind, visibility, summary, sourceRefs, validTime, systemTime, status }) {
  return { id, version, kind, visibility, summary, sourceRefs, validTime, systemTime, status };
}
const actions = Object.freeze({
  defaults: [], sessions: [], 'create-session': ['workspaceRoot'], configure: ['sessionId', 'input', 'expectedVersion'],
  config: ['sessionId'], status: ['sessionId'], memories: ['sessionId', 'filters'],
  memory: ['sessionId', 'memoryId', 'version'], 'semantic-graph': ['sessionId', 'filters'],
  'put-memory': ['sessionId', 'record', 'reason'], confirm: ['sessionId', 'memoryId', 'expectedVersion', 'reason'],
  search: ['sessionId', 'query', 'limit'], 'page-in': ['sessionId', 'memoryId', 'version', 'maxChars'],
  'page-out': ['sessionId', 'memoryId'], barrier: ['sessionId'], effects: ['sessionId', 'filters'], 'effect-graph': ['sessionId', 'filters'],
});

/** Explicit local operator surface: no dynamic service dispatch, execution or recovery. */
export function createMemoryRecoveryHandler(service, apiProxy) {
  return async (req, res) => {
    const send = (status, body, type = 'application/json') => {
      res.writeHead(status, { 'content-type': type, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer', 'content-security-policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'" });
      res.end(type === 'application/json' ? JSON.stringify(body) : body);
    };
    const authority = req.headers.host;
    if (!local(req.socket.remoteAddress) || ![`127.0.0.1:${req.socket.localPort}`, `localhost:${req.socket.localPort}`, `[::1]:${req.socket.localPort}`].includes(authority)
      || (req.headers.origin && req.headers.origin !== `http://${authority}`)
      || (req.headers['sec-fetch-site'] && !['none', 'same-origin'].includes(req.headers['sec-fetch-site']))) return send(403, { code: 'LOCAL_SAME_ORIGIN_REQUIRED' });
    const path = new URL(req.url, `http://${authority}`).pathname;
    try {
      if (path === '/memory-recovery' && req.method === 'GET') return send(200, await readFile(page, 'utf8'), 'text/html; charset=utf-8');
      const action = path.slice('/memory-recovery/api/'.length);
      if (!path.startsWith('/memory-recovery/api/') || !Object.hasOwn(actions, action)) return send(404, { code: 'MEMORY_ROUTE_NOT_FOUND' });
      if (req.method !== 'POST') return send(405, { code: 'METHOD_NOT_ALLOWED' });
      if (req.headers.origin !== `http://${authority}` || req.headers['content-type']?.split(';')[0] !== 'application/json') return send(403, { code: 'LOCAL_SAME_ORIGIN_JSON_REQUIRED' });
      let size = 0; const chunks = [];
      for await (const chunk of req) { size += chunk.length; if (size > 262144) return send(413, { code: 'MEMORY_REQUEST_TOO_LARGE' }); chunks.push(chunk); }
      const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      fields(data, actions[action]);
      if (actions[action].includes('sessionId') && (typeof data.sessionId !== 'string' || !data.sessionId)) invalid();
      if (actions[action].includes('memoryId') && (typeof data.memoryId !== 'string' || !data.memoryId)) invalid();
      if (data.filters !== undefined) fields(data.filters, action === 'memories' ? ['kind', 'visibility', 'status', 'limit'] : ['validAt', 'systemAt', ...(action === 'semantic-graph' ? [] : ['limit'])]);
      let value; const id = data.sessionId;
      switch (action) {
        case 'defaults': {
          const response = await apiProxy.host.describe({ rpcId: randomUUID(), payload: {} });
          const cwd = response.result.ok && response.result.value.cwd;
          if (typeof cwd !== 'string' || !isAbsolute(cwd)) throw Object.assign(new Error('Native defaults unavailable'), { code: 'MEMORY_NATIVE_DEFAULTS_FAILED' });
          value = { workspaceRoot: cwd }; break;
        }
        case 'sessions': value = service.listSessions(); break;
        case 'create-session': {
          if (typeof data.workspaceRoot !== 'string' || !isAbsolute(data.workspaceRoot)) invalid();
          const cwd = await realpath(data.workspaceRoot); if (!(await stat(cwd)).isDirectory()) invalid();
          // Native API remains the creation authority. Never adopt a caller-selected old id.
          const sessionId = `session-${randomUUID()}`;
          const response = await apiProxy.sessions.create({ rpcId: randomUUID(), payload: { sessionId, cwd } });
          if (!response.result.ok) throw Object.assign(new Error('Native session creation failed'), { code: 'MEMORY_NATIVE_CREATE_FAILED' });
          value = { sessionId: response.result.value.sessionId, workspaceRoot: cwd }; break;
        }
        case 'configure': value = await service.configureSession(id, data.input, { expectedVersion: data.expectedVersion }); break;
        case 'config': value = service.getSessionConfig(id); break;
        case 'status': value = service.status(id); break;
        case 'memories': value = service.listMemories(id, data.filters).map(summaryHandle); break;
        case 'memory': value = service.getMemory(id, data.memoryId, data.version); break;
        case 'semantic-graph': value = service.semanticGraph(id, data.filters); break;
        case 'put-memory': value = await service.putOperatorMemory(id, data.record, data.reason); break;
        case 'confirm': value = await service.confirmProcedure(id, data.memoryId, { expectedVersion: data.expectedVersion, reason: data.reason }); break;
        case 'search': value = service.search(id, data.query, { limit: data.limit }); break;
        case 'page-in': value = service.pageIn(id, data.memoryId, { version: data.version, maxChars: data.maxChars }); break;
        case 'page-out': value = service.pageOut(id, data.memoryId); break;
        case 'barrier': await service.barrier(id); value = service.status(id); break;
        case 'effects': value = service.listEffects(id, data.filters); break;
        case 'effect-graph': value = service.effectGraph(id, data.filters); break;
      }
      return send(200, { value });
    } catch (cause) {
      const code = /^(MEMORY|EFFECT|SANDBOX)_[A-Z_]+$/.test(cause?.code) ? cause.code : 'MEMORY_OPERATION_FAILED';
      // Do not echo native exceptions, paths, request bodies or secrets.
      return send(400, { code, error: '操作未完成。请检查输入、会话范围及版本；历史不会被删除。' });
    }
  };
}

export function installMemoryRecoveryUi(ctx, service) {
  const handler = createMemoryRecoveryHandler(service, ctx.apiProxy);
  ctx.effect(() => ctx.webServer.register({ kind: 'prefix', path: '/memory-recovery', handler }));
  ctx.effect(() => ctx.webServer.tapIndex(html => html.replace('</body>', '<a href="/memory-recovery" target="_blank" rel="noopener" style="position:fixed;bottom:44px;right:12px;z-index:99999;font:12px sans-serif;background:#fff;color:#17304a;padding:6px 10px;border:1px solid #ccc;border-radius:6px">记忆 / 恢复 / 沙箱</a></body>')));
}

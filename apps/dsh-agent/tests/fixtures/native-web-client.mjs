import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';

const require = createRequire(new URL('../../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url));

export function redactLaunchOutput(output) {
  return output.replace(/([?&]token=)[^\s&)]+/g, '$1[redacted]');
}

/** Exchange this process's startup URL; keep its token and cookie in memory. */
export async function authenticateWeb(output, origin, { fetch: send = globalThis.fetch, WebSocket: Socket } = {}) {
  const launchUrl = [...output.matchAll(/dsh web: (http:\/\/[^\s]+)/g)]
    .map(match => match[1]).find(value => {
      const url = new URL(value);
      return url.origin === origin && url.searchParams.has('token');
    });
  assert.ok(launchUrl, 'Native startup must print an authenticated URL for this origin');
  const login = await send(launchUrl, { redirect: 'manual', signal: AbortSignal.timeout(10000) });
  assert.equal(login.status, 303, 'Native launch-token exchange must redirect');
  assert.equal(new URL(login.headers.get('location'), origin).href, `${origin}/`);
  const cookie = login.headers.get('set-cookie')?.split(';', 1)[0];
  assert.ok(cookie, 'Native launch-token exchange must issue a browser cookie');

  async function authenticatedFetch(path, init = {}) {
    const url = new URL(path, origin);
    assert.equal(url.origin, origin, 'Native test credentials must remain on their origin');
    const headers = new Headers(init.headers);
    headers.set('cookie', cookie);
    if (!headers.has('origin')) headers.set('origin', origin);
    return send(url, { signal: AbortSignal.timeout(10000), ...init, headers });
  }
  async function rpc(endpoint, args) {
    const rpcId = randomUUID();
    const response = await authenticatedFetch(`/api/${endpoint}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'client-request', rpcId, method: endpoint, payload: { args } }),
    });
    assert.equal(response.status, 200, `${endpoint} HTTP status`);
    const body = await response.json();
    assert.equal(body.type, 'server-response');
    assert.equal(body.rpcId, rpcId, `${endpoint} response must match its request`);
    assert.equal(body.result.ok, true, JSON.stringify(body));
    return body.result.value;
  }
  async function firstFrame(endpoint, args) {
    const WebSocket = Socket ?? (await import(require.resolve('ws'))).default;
    const socket = new WebSocket(`${origin.replace(/^http/, 'ws')}/api/remote.mux`, {
      headers: { cookie, origin },
    });
    const streamId = randomUUID();
    try {
      return await new Promise((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => finish(new Error(`${endpoint} opening frame timed out`)), 10000);
        function finish(error, value) {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (error) reject(error); else resolve(value);
        }
        socket.once('open', () => socket.send(JSON.stringify({ type: 'open', streamId, endpoint, payload: { args } })));
        socket.on('error', () => finish(new Error(`${endpoint} stream connection failed`)));
        socket.once('close', () => finish(new Error(`${endpoint} closed before its opening frame`)));
        socket.on('message', bytes => {
          try {
            const frame = JSON.parse(bytes.toString());
            if (frame.streamId !== streamId) return;
            if (frame.type === 'error') finish(new Error(`${endpoint}: ${JSON.stringify(frame.error)}`));
            else if (frame.type === 'end') finish(new Error(`${endpoint} ended before its opening frame`));
            else if (frame.type === 'item') finish(undefined, frame.value);
          } catch (error) { finish(error); }
        });
      });
    } finally {
      if (socket.readyState === WebSocket.OPEN) socket.close();
      else if (socket.readyState !== WebSocket.CLOSED) socket.terminate();
    }
  }
  async function snapshot(sessionId) {
    const frame = await firstFrame('session/follow', { request: { address: { kind: 'session', sessionId } } });
    assert.equal(frame.type, 'snapshot');
    assert.equal(frame.header.id, sessionId);
    assert.ok(Number.isSafeInteger(frame.cursor));
    return frame;
  }
  async function history(sessionId) {
    const opening = await snapshot(sessionId);
    const request = { address: { kind: 'session', sessionId }, throughSeq: opening.cursor, maxMessages: 100 };
    let page = await rpc('session/page', { request });
    let records = page.records;
    while (page.hasMore) {
      const beforeSeq = records[0]?.event.seq;
      assert.ok(Number.isSafeInteger(beforeSeq), 'History pagination must make progress');
      page = await rpc('session/page', { request: { ...request, beforeSeq } });
      assert.ok(page.records.length > 0 && page.records.at(-1).event.seq < beforeSeq, 'History pages must precede the current cursor');
      records = [...page.records, ...records];
    }
    return { records, hasMore: false };
  }
  async function authenticateBrowser(context) {
    const separator = cookie.indexOf('=');
    assert.ok(separator > 0, 'Native browser cookie must have a name');
    await context.addCookies([{ name: cookie.slice(0, separator), value: cookie.slice(separator + 1),
      url: origin, httpOnly: true, sameSite: 'Strict', secure: new URL(origin).protocol === 'https:' }]);
  }
  return { origin, cookie, fetch: authenticatedFetch, rpc, firstFrame, snapshot, history, authenticateBrowser };
}

/** Observe both unary requests and multiplexed stream opens made by the real UI. */
export function captureNativeRequests(page) {
  const requests = [];
  const record = raw => {
    try {
      const request = JSON.parse(raw);
      if (request?.type === 'client-request' || request?.type === 'open') requests.push(request);
    } catch {}
  };
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) record(request.postData());
  });
  page.on('websocket', socket => {
    if (new URL(socket.url()).pathname === '/api/remote.mux') socket.on('framesent', frame => record(frame.payload.toString()));
  });
  return requests;
}

export function assertSessionRequest(requests, sessionId) {
  assert.ok(requests.some(request => {
    if (request.type !== 'client-request' && request.type !== 'open') return false;
    const endpoint = request.type === 'open' ? request.endpoint : request.method;
    const id = request.type === 'open' ? request.streamId : request.rpcId;
    const input = request.payload?.args?.request;
    return typeof id === 'string' && id.length > 0 && typeof endpoint === 'string' && endpoint.includes('/')
      && (input?.sessionId === sessionId || (input?.address?.kind === 'session' && input.address.sessionId === sessionId));
  }), `The browser must request session ${sessionId} through a correlated native envelope`);
}

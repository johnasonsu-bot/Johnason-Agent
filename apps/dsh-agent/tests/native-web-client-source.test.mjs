import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { authenticateWeb, redactLaunchOutput, captureNativeRequests, assertSessionRequest } from './fixtures/native-web-client.mjs';

const origin = 'http://127.0.0.1:43210';
function transportFixture({ rpc, frame } = {}) {
  const token = randomUUID(), cookie = `dsh-test=${randomUUID()}`;
  const requests = [], sockets = [];
  class Socket extends EventEmitter {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = 0;
    constructor(url, options) {
      super();
      assert.equal(url, 'ws://127.0.0.1:43210/api/remote.mux');
      assert.equal(options.headers.cookie, cookie);
      assert.equal(options.headers.origin, origin);
      sockets.push(this);
      queueMicrotask(() => { this.readyState = Socket.OPEN; this.emit('open'); });
    }
    send(bytes) {
      const request = JSON.parse(bytes);
      requests.push(request);
      assert.equal(request.type, 'open');
      assert.ok(request.streamId);
      queueMicrotask(() => {
        this.emit('message', Buffer.from(JSON.stringify({ type: 'item', streamId: 'unrelated', value: null })));
        const response = frame(request);
        this.emit('message', Buffer.from(JSON.stringify({ streamId: request.streamId, ...response })));
      });
    }
    close() { this.readyState = Socket.CLOSED; this.emit('close'); }
    terminate() { this.close(); }
  }
  const fetch = async (input, init) => {
    const url = new URL(input);
    if (url.searchParams.has('token')) {
      assert.equal(url.origin, origin);
      assert.equal(url.searchParams.get('token'), token);
      assert.equal(init.redirect, 'manual');
      return new Response(null, { status: 303, headers: { location: '/', 'set-cookie': `${cookie}; Path=/; HttpOnly; SameSite=Strict` } });
    }
    assert.equal(url.origin, origin);
    assert.equal(init.headers.get('cookie'), cookie);
    const request = JSON.parse(init.body);
    requests.push(request);
    assert.equal(url.pathname, `/api/${request.method}`);
    const response = rpc(request, init);
    return Response.json({ type: 'server-response', rpcId: request.rpcId, result: { ok: true, value: response } });
  };
  const output = `dsh web: http://127.0.0.1:1/?token=other-process\ndsh web: ${origin}/?token=${token}\n`;
  return { token, cookie, requests, sockets, output, options: { fetch, WebSocket: Socket } };
}

test('native helper exchanges only its origin token, correlates RPC envelopes and keeps browser authentication in memory', async () => {
  const fixture = transportFixture({ rpc(request, init) {
    assert.equal(init.headers.get('origin'), origin);
    assert.deepEqual(request.payload, { args: { _request: {} } });
    assert.equal(request.type, 'client-request');
    assert.equal(typeof request.rpcId, 'string');
    return { items: [] };
  } });
  const client = await authenticateWeb(fixture.output, origin, fixture.options);
  assert.deepEqual(await client.rpc('session/list', { _request: {} }), { items: [] });
  const cookies = [];
  await client.authenticateBrowser({ async addCookies(values) { cookies.push(...values); } });
  assert.deepEqual(cookies, [{ name: 'dsh-test', value: fixture.cookie.split('=')[1], url: origin, httpOnly: true, sameSite: 'Strict', secure: false }]);
  assert.equal(Object.hasOwn(client, 'launchUrl'), false);
  await assert.rejects(client.fetch('https://unrelated.example/api/session/list'), /remain on their origin/);
  assert.equal(fixture.requests.length, 1);
});

test('native helper refuses an unrelated RPC response identifier', async () => {
  const fixture = transportFixture();
  const fetch = async (url, init) => new URL(url).searchParams.has('token')
    ? fixture.options.fetch(url, init)
    : Response.json({ type: 'server-response', rpcId: 'unrelated', result: { ok: true, value: {} } });
  const client = await authenticateWeb(fixture.output, origin, { ...fixture.options, fetch });
  await assert.rejects(client.rpc('session/list', { _request: {} }), /response must match its request/);
});

test('native history uses the follow cursor and pages backward without losing or duplicating events', async () => {
  const sessionId = 'source-history';
  const record = seq => ({ type: 'event', event: { seq, type: 'turn/end', data: {} } });
  const fixture = transportFixture({
    frame(request) {
      assert.equal(request.endpoint, 'session/follow');
      assert.deepEqual(request.payload, { args: { request: { address: { kind: 'session', sessionId } } } });
      return { type: 'item', value: { type: 'snapshot', header: { id: sessionId }, cursor: 5, records: [record(5)] } };
    },
    rpc(request) {
      assert.equal(request.method, 'session/page');
      const { request: input } = request.payload.args;
      assert.deepEqual(input.address, { kind: 'session', sessionId });
      assert.equal(input.throughSeq, 5);
      assert.equal(input.maxMessages, 100);
      if (input.beforeSeq === undefined) return { records: [record(4), record(5)], hasMore: true };
      assert.equal(input.beforeSeq, 4);
      return { records: [record(1), record(2), record(3)], hasMore: false };
    },
  });
  const client = await authenticateWeb(fixture.output, origin, fixture.options);
  assert.deepEqual((await client.history(sessionId)).records.map(entry => entry.event.seq), [1, 2, 3, 4, 5]);
  assert.ok(fixture.sockets.every(socket => socket.readyState === 3));
});

test('native helper rejects non-progressing history and reports stream errors while closing sockets', async () => {
  const fixture = transportFixture({
    frame: () => ({ type: 'item', value: { type: 'snapshot', header: { id: 'source-stalled' }, cursor: 1 } }),
    rpc: () => ({ records: [{ event: { seq: 1 } }], hasMore: true }),
  });
  const client = await authenticateWeb(fixture.output, origin, fixture.options);
  await assert.rejects(client.history('source-stalled'), /must precede the current cursor/);
  const failure = transportFixture({ frame: () => ({ type: 'error', error: { code: 'stream/failed' } }) });
  const failedClient = await authenticateWeb(failure.output, origin, failure.options);
  await assert.rejects(failedClient.firstFrame('workspace/follow', {}), /stream\/failed/);
  assert.ok([...fixture.sockets, ...failure.sockets].every(socket => socket.readyState === 3));
});

test('browser capture accepts correlated unary and stream session requests and rejects old envelopes', () => {
  const page = new EventEmitter(), socket = new EventEmitter();
  const requests = captureNativeRequests(page);
  const sessionId = 'source-browser';
  page.emit('request', { url: () => `${origin}/api/skills/list`, postData: () => JSON.stringify({
    type: 'client-request', rpcId: 'rpc-1', method: 'skills/list', payload: { args: { request: { sessionId } } },
  }) });
  assertSessionRequest(requests, sessionId);
  requests.length = 0;
  socket.url = () => 'ws://127.0.0.1:43210/api/remote.mux';
  page.emit('websocket', socket);
  socket.emit('framesent', { payload: JSON.stringify({ type: 'open', streamId: 'stream-1', endpoint: 'session/follow',
    payload: { args: { request: { address: { kind: 'session', sessionId } } } } }) });
  assertSessionRequest(requests, sessionId);
  assert.throws(() => assertSessionRequest([{ type: 'client-request', rpcId: 'legacy', method: 'session.history', payload: { sessionId } }], sessionId), /correlated native envelope/);
  assert.throws(() => assertSessionRequest(requests, 'different-session'), /correlated native envelope/);
});

test('startup diagnostic redaction removes launch tokens while retaining origins and useful logs', () => {
  const fixture = transportFixture();
  const redacted = redactLaunchOutput(`${fixture.output}SQLite warning\n${origin}/?next=home&token=${fixture.token}&mode=web`);
  assert.equal(redacted.includes(fixture.token), false);
  assert.equal(redacted.includes('other-process'), false);
  assert.match(redacted, /token=\[redacted\]&mode=web/);
  assert.match(redacted, /SQLite warning/);
  assert.ok(redacted.includes(origin));
});

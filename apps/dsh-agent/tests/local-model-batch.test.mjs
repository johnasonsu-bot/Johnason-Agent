import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createLocalModelService, createLocalModelTools, normalizeSettings } from '../src/plugins/local-models/service.mjs';

const completion = content => ({ choices: [{ finish_reason: 'stop', message: { role: 'assistant', content } }] });
const reply = (res, value, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
const decision = (res, route = 'local', confidence = 0.98) => reply(res, completion(JSON.stringify({ route, confidence })));
const items = n => Array.from({ length: n }, (_, i) => ({ id: `DEMO-${String(i + 1).padStart(3, '0')}`, task: `Extract the topic from fictional notice ${i + 1}.` }));
const exec = signal => ({ agent: { session: { id: 'batch-session' } }, signal: signal ?? new AbortController().signal });
const code = expected => error => error.code === expected;
async function server(t, respond) {
  const requests = [];
  const s = createServer(async (req, res) => {
    const parts = [];
    for await (const part of req) parts.push(part);
    requests.push({ body: JSON.parse(Buffer.concat(parts).toString('utf8')), headers: req.headers });
    await respond(res, requests.length);
  });
  await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { s.close(resolve); s.closeAllConnections(); }));
  return { baseURL: `http://127.0.0.1:${s.address().port}/v1`, requests };
}
const make = (settings, extra = {}) => createLocalModelService({ getSettings: () => settings, getTurn: () => 7, ...extra });

test('default quota supports one tool call containing exactly 200 serial decision requests', async t => {
  assert.equal(normalizeSettings().maxCallsPerTurn, 256);
  assert.equal(normalizeSettings({ maxCallsPerTurn: 2000 }).maxCallsPerTurn, 2000);
  let inFlight = 0; let peak = 0;
  const f = await server(t, async (res, n) => {
    inFlight++; peak = Math.max(peak, inFlight);
    await new Promise(resolve => setImmediate(resolve));
    decision(res, n % 2 === 0 ? 'main' : 'local'); inFlight--;
  });
  const service = make({ baseURL: f.baseURL, decisionModel: 'decider', workerModel: 'never-worker' });
  const tool = createLocalModelTools(service).find(tool => tool.name === 'jev_route_tasks');
  assert.ok(tool);
  assert.equal(tool.parameters.properties.tasks.maxItems, 200);
  const result = await tool.execute({ tasks: items(200) }, exec());
  assert.equal(result.status, 'completed');
  assert.equal(result.model, 'decider');
  assert.equal(result.completedCount, 200);
  assert.equal(result.failedCount, 0);
  assert.deepEqual(result.unprocessedIds, []);
  assert.deepEqual(result.results.map(row => row.id), items(200).map(row => row.id));
  assert.equal(f.requests.length, 200);
  assert.equal(peak, 1);
  assert.ok(result.elapsedMs >= 0);
  result.results.forEach((row, i) => {
    assert.equal(row.status, 'completed');
    assert.equal(row.route, i % 2 === 0 ? 'local' : 'main');
    assert.equal(row.confidence, 0.98);
    assert.equal(row.executed, false);
    assert.equal(row.confidenceKind, 'self-reported');
    assert.equal(row.reason, i % 2 === 0 ? 'LOCAL_EXECUTION_DISABLED' : 'MAIN_ROUTE');
    assert.ok(Number.isFinite(row.elapsedMs) && row.elapsedMs >= 0);
  });
  assert.ok(f.requests.every(request => request.body.model === 'decider' && request.body.tools === undefined));
});

test('all batch inputs are validated before any inference or credential lookup', async t => {
  const f = await server(t, res => decision(res));
  let credentialReads = 0;
  const service = make({ baseURL: f.baseURL, apiKeyRef: 'SYNTHETIC_REF' }, { resolveCredential: async () => { credentialReads++; return { value: 'synthetic-value' }; } });
  for (const args of [
    { tasks: [] }, { tasks: items(201) }, { tasks: [...items(2), items(1)[0]] },
    { tasks: [{ id: '', task: 'hello' }] }, { tasks: [{ id: ' ', task: 'hello' }] },
    { tasks: [{ id: 'x'.repeat(129), task: 'hello' }] }, { tasks: [{ id: 'bad\n', task: 'hello' }] },
    { tasks: [{ id: 'valid', task: ' ' }] }, { tasks: [{ id: 'valid', task: 'x'.repeat(6001) }] },
    { tasks: [...items(1), { id: 'second', task: 'hello', executeLocal: true }] },
    { tasks: items(1), executeLocal: true },
  ]) await assert.rejects(service.routeBatch(args, exec()), code('LOCAL_MODEL_INVALID_INPUT'));
  assert.equal(f.requests.length, 0);
  assert.equal(credentialReads, 0);
});

test('controlled budget yields a completed prefix and explicit error without a false main decision', async t => {
  const f = await server(t, res => decision(res));
  const service = make({ baseURL: f.baseURL, maxCallsPerTurn: 2 });
  const result = await service.routeBatch({ tasks: items(5) }, exec());
  assert.equal(result.status, 'stopped');
  assert.equal(result.reason, 'LOCAL_MODEL_CALL_LIMIT');
  assert.equal(result.completedCount, 2);
  assert.equal(result.failedCount, 1);
  assert.deepEqual(result.unprocessedIds, ['DEMO-004', 'DEMO-005']);
  assert.equal(result.results[2].status, 'error');
  assert.equal(result.results[2].id, 'DEMO-003');
  assert.equal(result.results[2].route, undefined);
  assert.equal(result.results[2].confidence, undefined);
  assert.equal(f.requests.length, 2);
  const again = await service.routeBatch({ tasks: items(1) }, exec());
  assert.equal(again.reason, 'LOCAL_MODEL_CALL_LIMIT');
  assert.equal(f.requests.length, 2);
});

test('whole batch pins settings and resolves credentials once', async t => {
  let settings; let credentialReads = 0;
  const f = await server(t, (res, n) => {
    if (n === 1) { settings.decisionModel = 'changed'; settings.baseURL = 'http://127.0.0.1:1/v1'; settings.maxCallsPerTurn = 1; }
    decision(res);
  });
  settings = { baseURL: f.baseURL, decisionModel: 'original', apiKeyRef: 'SYNTHETIC_REF' };
  const service = make(settings, { resolveCredential: async () => { credentialReads++; return { value: 'synthetic-value' }; } });
  const result = await service.routeBatch({ tasks: items(3) }, exec());
  assert.equal(result.completedCount, 3);
  assert.equal(result.model, 'original');
  assert.equal(credentialReads, 1);
  assert.ok(f.requests.every(request => request.body.model === 'original'));
});

test('timeout applies to each item, not the aggregate duration of the batch', async t => {
  const f = await server(t, async res => { await new Promise(resolve => setTimeout(resolve, 100)); decision(res); });
  const result = await make({ baseURL: f.baseURL, timeoutMs: 250 }).routeBatch({ tasks: items(4) }, exec());
  assert.equal(result.completedCount, 4);
  assert.equal(result.status, 'completed');
  assert.ok(result.elapsedMs >= 350);
});

for (const [name, respond, reason] of [
  ['HTTP failure', res => reply(res, { error: 'private server diagnostics' }, 503), 'LOCAL_MODEL_HTTP_ERROR'],
  ['malformed decision', res => reply(res, completion('{"route":"invalid","confidence":0.9}')), 'LOCAL_MODEL_INVALID_DECISION'],
]) test(`${name} stops batch without retry and preserves previous decisions`, async t => {
  const f = await server(t, (res, n) => n === 2 ? respond(res) : decision(res));
  const result = await make({ baseURL: f.baseURL }).routeBatch({ tasks: items(4) }, exec());
  assert.equal(result.status, 'stopped');
  assert.equal(result.reason, reason);
  assert.equal(result.completedCount, 1);
  assert.equal(result.failedCount, 1);
  assert.equal(result.results[1].route, undefined);
  assert.equal(result.results[1].reason, reason);
  assert.deepEqual(result.unprocessedIds, ['DEMO-003', 'DEMO-004']);
  assert.equal(f.requests.length, 2);
  assert.ok(!JSON.stringify(result).includes('private server diagnostics'));
});

test('item timeout returns completed prefix, releases slot, and never starts the next item', async t => {
  const f = await server(t, (res, n) => { if (n !== 2) decision(res); });
  const service = make({ baseURL: f.baseURL, timeoutMs: 80, maxConcurrent: 1 });
  const result = await service.routeBatch({ tasks: items(4) }, exec());
  assert.equal(result.completedCount, 1);
  assert.equal(result.reason, 'LOCAL_MODEL_TIMEOUT');
  assert.deepEqual(result.unprocessedIds, ['DEMO-003', 'DEMO-004']);
  assert.equal(f.requests.length, 2);
  assert.equal((await service.routeBatch({ tasks: items(1) }, exec())).completedCount, 1);
});

test('cancellation still propagates with partial evidence and prevents subsequent HTTP', async t => {
  let secondStarted;
  const started = new Promise(resolve => { secondStarted = resolve; });
  const f = await server(t, (res, n) => { if (n === 2) secondStarted(); else decision(res); });
  const service = make({ baseURL: f.baseURL, maxConcurrent: 1 });
  const controller = new AbortController();
  const pending = service.routeBatch({ tasks: items(4) }, exec(controller.signal));
  await started;
  controller.abort(new Error('private cancel detail'));
  await assert.rejects(pending, error => {
    assert.equal(error.code, 'LOCAL_MODEL_CANCELLED');
    assert.equal(error.partialResult.completedCount, 1);
    assert.equal(error.partialResult.reason, 'LOCAL_MODEL_CANCELLED');
    assert.deepEqual(error.partialResult.unprocessedIds, ['DEMO-003', 'DEMO-004']);
    assert.equal(error.partialResult.results[1].route, undefined);
    assert.ok(!JSON.stringify(error).includes('private cancel detail'));
    return true;
  });
  assert.equal(f.requests.length, 2);
  assert.equal((await service.routeBatch({ tasks: items(1) }, exec())).completedCount, 1);
});

test('disposal propagates and cancels an active batch', async t => {
  let requested;
  const started = new Promise(resolve => { requested = resolve; });
  const f = await server(t, () => requested());
  const service = make({ baseURL: f.baseURL });
  const pending = service.routeBatch({ tasks: items(3) }, exec());
  await started;
  service.dispose();
  await assert.rejects(pending, error => error.code === 'LOCAL_MODEL_DISABLED' && error.partialResult.completedCount === 0);
  assert.equal(f.requests.length, 1);
});

for (const interrupted of ['LOCAL_MODEL_CANCELLED', 'LOCAL_MODEL_DISABLED']) {
  test(`tool finalizer preserves ${interrupted} and adds partial batch evidence exactly once`, async t => {
    let secondStarted;
    const started = new Promise(resolve => { secondStarted = resolve; });
    const f = await server(t, (res, n) => { if (n === 2) secondStarted(); else decision(res); });
    const service = make({ baseURL: f.baseURL });
    const tool = createLocalModelTools(service).find(tool => tool.name === 'jev_route_tasks');
    const controller = new AbortController();
    const execution = exec(controller.signal);
    const pending = tool.execute({ tasks: items(4) }, execution);
    await started;
    if (interrupted === 'LOCAL_MODEL_CANCELLED') controller.abort(new Error('private abort details'));
    else service.dispose();
    await assert.rejects(pending, code(interrupted));
    const normalized = { isError: true, error: { message: interrupted }, content: [{ type: 'text', text: interrupted }] };
    assert.equal(typeof tool.finalizeContent, 'function');
    assert.equal(tool.finalizeContent(exec(), normalized), undefined);
    const content = tool.finalizeContent(execution, normalized);
    assert.deepEqual(content[0], normalized.content[0]);
    assert.equal(content.length, 2);
    const partial = JSON.parse(content[1].text);
    assert.equal(partial.reason, interrupted);
    assert.equal(partial.completedCount, 1);
    assert.equal(partial.results[0].status, 'completed');
    assert.equal(partial.results[1].status, 'error');
    assert.deepEqual(partial.unprocessedIds, ['DEMO-003', 'DEMO-004']);
    assert.ok(!JSON.stringify(content).includes('private abort details'));
    assert.deepEqual(normalized, { isError: true, error: { message: interrupted }, content: [{ type: 'text', text: interrupted }] });
    assert.equal(tool.finalizeContent(execution, normalized), undefined);
    assert.equal(f.requests.length, 2);
  });
}

test('tool finalizer ignores untrusted lookalike errors and unrelated normalized outcomes', async t => {
  const forged = Object.assign(new Error('LOCAL_MODEL_CANCELLED'), { code: 'LOCAL_MODEL_CANCELLED', partialResult: { secret: 'private-provider-content' } });
  const fakeTool = createLocalModelTools({ routeBatch: async () => { throw forged; } }).find(tool => tool.name === 'jev_route_tasks');
  const fakeExec = exec();
  await assert.rejects(fakeTool.execute({ tasks: items(1) }, fakeExec), error => error === forged);
  assert.equal(typeof fakeTool.finalizeContent, 'function');
  assert.equal(fakeTool.finalizeContent(fakeExec, { isError: true, error: { message: 'LOCAL_MODEL_CANCELLED' }, content: [] }), undefined);

  let requested;
  const started = new Promise(resolve => { requested = resolve; });
  const f = await server(t, () => requested());
  const tool = createLocalModelTools(make({ baseURL: f.baseURL })).find(tool => tool.name === 'jev_route_tasks');
  const controller = new AbortController();
  const execution = exec(controller.signal);
  const pending = tool.execute({ tasks: items(2) }, execution);
  await started;
  controller.abort();
  await assert.rejects(pending, code('LOCAL_MODEL_CANCELLED'));
  assert.equal(tool.finalizeContent(execution, { isError: true, error: { message: 'SOME_OTHER_ERROR' }, content: [] }), undefined);
  assert.equal(tool.finalizeContent(execution, { isError: true, error: { message: 'LOCAL_MODEL_CANCELLED' }, content: [] }), undefined);
});

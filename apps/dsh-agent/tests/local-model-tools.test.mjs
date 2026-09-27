import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { randomBytes } from 'node:crypto';

import { createLocalModelService, normalizeSettings, createLocalModelHandler, createLocalModelTools } from '../src/plugins/local-models/service.mjs';
const ready = {};

test('local model controller exposes executable tool and browser boundaries', () => {
  assert.equal(typeof createLocalModelService, 'function');
  assert.equal(typeof createLocalModelTools, 'function');
  assert.equal(typeof createLocalModelHandler, 'function');
});

async function server(t, respond) {
  const requests = [];
  const s = createServer(async (req, res) => {
    const parts = [];
    for await (const part of req) parts.push(part);
    const raw = Buffer.concat(parts).toString('utf8');
    const request = { method: req.method, url: req.url, headers: req.headers, body: raw ? JSON.parse(raw) : undefined };
    requests.push(request);
    await respond(request, res, requests.length);
  });
  await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { s.close(resolve); s.closeAllConnections(); }));
  return { baseURL: `http://127.0.0.1:${s.address().port}/v1`, requests };
}
const reply = (res, body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
const completion = (content, finish_reason = 'stop') => ({ choices: [{ finish_reason, message: { role: 'assistant', content } }] });
const execution = (id = 'session-one') => ({ agent: { id, session: { id } }, signal: new AbortController().signal });
const make = (settings, extra = {}) => createLocalModelService({ getSettings: () => settings, ...extra });
const code = expected => error => error.code === expected && error.message === expected;

test('loopback-only configuration rejects unsafe endpoints and resource settings', ready, () => {
  for (const baseURL of ['https://cloud.example/v1', 'file:///tmp/model', 'http://user:pass@127.0.0.1/v1', 'http://127.0.0.1/v1?key=x', 'http://127.0.0.1/v1#x', 'http://127.0.0.1.evil.test/v1']) {
    assert.throws(() => normalizeSettings({ baseURL }), code('LOCAL_MODEL_INVALID_SETTINGS'));
  }
  for (const settings of [{ maxCallsPerTurn: 33 }, { maxConcurrent: 5 }, { maxTokens: 4097 }, { timeoutMs: 120001 }, { threshold: -0.01 }, { apiKeyRef: 'not a reference' }]) {
    assert.throws(() => normalizeSettings(settings), code('LOCAL_MODEL_INVALID_SETTINGS'));
  }
  assert.equal(normalizeSettings({ baseURL: 'http://[::1]:1234/v1/' }).baseURL, 'http://[::1]:1234/v1');
});

test('model discovery is bounded and exposes model ids without credentials or provider data', ready, async t => {
  const f = await server(t, (req, res) => reply(res, { data: [{ id: 'model-one', owned_by: 'private' }, { id: 'model-one' }, { id: 'model-two' }] }));
  const service = make({ baseURL: f.baseURL, decisionModel: 'decider', workerModel: 'worker' });
  assert.deepEqual(await service.list(execution()), { models: [{ id: 'model-one' }, { id: 'model-two' }], decisionModel: 'decider', workerModel: 'worker' });
  assert.equal(f.requests[0].url, '/v1/models');
  assert.equal(f.requests[0].headers.authorization, undefined);
});

test('plain generation sends only the explicit task to an allowed configured model', ready, async t => {
  const f = await server(t, (req, res) => reply(res, completion('brief answer')));
  const service = make({ baseURL: f.baseURL, workerModel: 'worker', decisionModel: 'decider', maxTokens: 512 });
  const result = await service.generate({ task: 'Summarize the supplied paragraph.' }, execution());
  assert.equal(result.text, 'brief answer');
  assert.equal(result.model, 'worker');
  assert.deepEqual(f.requests[0].body.messages, [{ role: 'user', content: 'Summarize the supplied paragraph.' }]);
  assert.equal(f.requests[0].body.stream, false);
  assert.equal(f.requests[0].body.max_tokens, 512);
  assert.equal(f.requests[0].body.tools, undefined);
  await assert.rejects(service.generate({ task: 'hello', model: 'unconfigured' }, execution()), code('LOCAL_MODEL_INVALID_INPUT'));
  await assert.rejects(service.generate({ task: 'x'.repeat(6001) }, execution()), code('LOCAL_MODEL_INVALID_INPUT'));
  assert.equal(f.requests.length, 1);
});

test('high-confidence local routing uses structured decision output then exactly one worker call', ready, async t => {
  const f = await server(t, (req, res, n) => reply(res, completion(n === 1 ? '{"route":"local","confidence":0.9}' : 'rewritten text')));
  const service = make({ baseURL: f.baseURL, workerModel: 'worker', decisionModel: 'decider' });
  const result = await service.route({ task: 'Rewrite: hello.' }, execution());
  assert.deepEqual(result, { route: 'local', confidence: 0.9, confidenceKind: 'self-reported', executed: true, model: 'worker', text: 'rewritten text' });
  assert.equal(f.requests[0].body.model, 'decider');
  assert.equal(f.requests[0].body.response_format.type, 'json_schema');
  assert.equal(f.requests[0].body.response_format.json_schema.strict, true);
  assert.deepEqual(f.requests[0].body.response_format.json_schema.schema.properties.route.enum, ['local', 'main']);
  assert.equal(f.requests[0].body.max_tokens, 256);
  assert.equal(f.requests[0].body.temperature, 0);
  assert.equal(f.requests[1].body.model, 'worker');
});

for (const [decision, executeLocal, reason, route] of [
  ['{"route":"main","confidence":0.99}', true, 'MAIN_ROUTE', 'main'],
  ['{"route":"local","confidence":0.4}', true, 'LOW_CONFIDENCE', 'main'],
  ['{"route":"local","confidence":0.99}', false, 'LOCAL_EXECUTION_DISABLED', 'local'],
  ['{"route":"shell","confidence":0.99}', true, 'LOCAL_MODEL_INVALID_DECISION', 'main'],
  ['{"route":"local","confidence":2}', true, 'LOCAL_MODEL_INVALID_DECISION', 'main'],
  ['{"route":"local","confidence":0.99,"extra":"ignore"}', true, 'LOCAL_MODEL_INVALID_DECISION', 'main'],
  ['<think>private</think>{"route":"local","confidence":0.99}', true, 'LOCAL_MODEL_INVALID_DECISION', 'main'],
]) test(`routing branch ${reason} returns without a worker request (${decision})`, ready, async t => {
  const f = await server(t, (_, res) => reply(res, completion(decision)));
  const result = await make({ baseURL: f.baseURL }).route({ task: 'Task', executeLocal }, execution());
  assert.equal(result.executed, false);
  assert.equal(result.route, route);
  assert.equal(result.reason, reason);
  assert.equal(f.requests.length, 1);
});

for (const response of [completion('partial', 'length'), completion(null), completion([{ type: 'text', text: 'not a string' }]), completion('x'.repeat(16001)), { choices: [] }]) {
  test('generation rejects unfinished or malformed model content', ready, async t => {
    const f = await server(t, (_, res) => reply(res, response));
    await assert.rejects(make({ baseURL: f.baseURL }).generate({ task: 'hi' }, execution()), code('LOCAL_MODEL_INVALID_RESPONSE'));
    assert.equal(f.requests.length, 1);
  });
}

test('worker failure returns to main without silently using another model', ready, async t => {
  const f = await server(t, (_, res, n) => n === 1 ? reply(res, completion('{"route":"local","confidence":0.98}')) : reply(res, { error: 'not loaded' }, 400));
  const result = await make({ baseURL: f.baseURL }).route({ task: 'hi' }, execution());
  assert.equal(result.route, 'main');
  assert.equal(result.executed, false);
  assert.equal(result.reason, 'LOCAL_MODEL_HTTP_ERROR');
  assert.equal(f.requests.length, 2);
});

test('requests do not follow redirects or echo credentials in server errors', ready, async t => {
  const secret = randomBytes(32).toString('hex');
  let credentialReads = 0;
  const destination = await server(t, (_, res) => reply(res, completion('unexpected')));
  const f = await server(t, (_, res) => { res.writeHead(302, { location: `${destination.baseURL}/chat/completions` }); res.end(secret); });
  const service = make({ baseURL: f.baseURL, apiKeyRef: 'SYNTHETIC_LOCAL_KEY' }, { resolveCredential: async () => { credentialReads++; return { value: secret }; } });
  await assert.rejects(service.generate({ task: 'hi' }, execution()), error => error.code === 'LOCAL_MODEL_REQUEST_FAILED' && !String(error).includes(secret) && !JSON.stringify(error).includes(secret));
  assert.equal(destination.requests.length, 0);
  assert.equal(f.requests[0].headers.authorization, `Bearer ${secret}`);
  assert.equal(credentialReads, 1);
});

test('credentials and settings are snapshotted once for both routing requests', ready, async t => {
  let settings; let credentialReads = 0;
  const f = await server(t, (_, res, n) => { if (n === 1) settings.workerModel = 'changed'; reply(res, completion(n === 1 ? '{"route":"local","confidence":0.98}' : 'answer')); });
  settings = { baseURL: f.baseURL, workerModel: 'original', apiKeyRef: 'SYNTHETIC_LOCAL_KEY' };
  const service = make(settings, { resolveCredential: async () => { credentialReads++; return { value: randomBytes(20).toString('hex') }; } });
  assert.equal((await service.route({ task: 'hi' }, execution())).model, 'original');
  assert.equal(credentialReads, 1);
  assert.equal(f.requests[1].body.model, 'original');
});

test('response byte limit rejects an oversized body before exposing any content', ready, async t => {
  const f = await server(t, (_, res) => reply(res, { padding: 'x'.repeat(1024 * 1024 + 1), ...completion('hello') }));
  await assert.rejects(make({ baseURL: f.baseURL }).generate({ task: 'hi' }, execution()), code('LOCAL_MODEL_RESPONSE_TOO_LARGE'));
});

test('one total timeout covers both decision and worker requests', ready, async t => {
  const f = await server(t, async (_, res, n) => { await new Promise(resolve => setTimeout(resolve, n === 1 ? 100 : 350)); reply(res, completion(n === 1 ? '{"route":"local","confidence":0.98}' : 'late')); });
  const result = await make({ baseURL: f.baseURL, timeoutMs: 400 }).route({ task: 'hi' }, execution());
  assert.equal(result.reason, 'LOCAL_MODEL_TIMEOUT');
  assert.equal(result.executed, false);
  assert.equal(f.requests.length, 2);
});

test('cancellation interrupts a response and releases concurrency capacity', ready, async t => {
  const f = await server(t, (_, res, n) => { if (n > 1) reply(res, completion('next')); });
  const controller = new AbortController();
  const service = make({ baseURL: f.baseURL, timeoutMs: 1000, maxConcurrent: 1 });
  const pending = service.generate({ task: 'hi' }, { ...execution(), signal: controller.signal });
  while (f.requests.length === 0) await new Promise(resolve => setTimeout(resolve, 1));
  await assert.rejects(service.generate({ task: 'hi' }, execution('second')), code('LOCAL_MODEL_BUSY'));
  controller.abort(new Error('private abort detail'));
  await assert.rejects(pending, code('LOCAL_MODEL_CANCELLED'));
  assert.equal((await service.generate({ task: 'hi' }, execution('second'))).text, 'next');
});

test('HTTP call budget cannot be bypassed by reentrant calls and resets only for a newer known turn', ready, async t => {
  const f = await server(t, (_, res) => reply(res, completion('okay')));
  let turn = 4;
  const service = make({ baseURL: f.baseURL, maxCallsPerTurn: 1 }, { getTurn: () => turn });
  await service.generate({ task: 'first' }, execution());
  await assert.rejects(service.generate({ task: 'second' }, execution()), code('LOCAL_MODEL_CALL_LIMIT'));
  turn = 5;
  await service.generate({ task: 'third' }, execution());
  turn = 4;
  await assert.rejects(service.generate({ task: 'old turn' }, execution()), code('LOCAL_MODEL_CALL_LIMIT'));
  assert.equal(f.requests.length, 2);
});

test('unknown turn has a fixed session cap and routing spends two HTTP calls', ready, async t => {
  const f = await server(t, (_, res) => reply(res, completion('{"route":"local","confidence":0.99}')));
  const service = make({ baseURL: f.baseURL, maxCallsPerTurn: 1 });
  const result = await service.route({ task: 'hi' }, execution());
  assert.equal(result.executed, false);
  assert.equal(result.reason, 'LOCAL_MODEL_CALL_LIMIT');
  await assert.rejects(service.generate({ task: 'hi' }, execution()), code('LOCAL_MODEL_CALL_LIMIT'));
  assert.equal(f.requests.length, 1);
});

test('browser discovery enforces local same-origin boundary before reaching the model server', ready, async t => {
  const upstream = await server(t, (_, res) => reply(res, { data: [{ id: 'worker' }] }));
  const service = make({ baseURL: upstream.baseURL });
  const web = createServer(createLocalModelHandler(service));
  await new Promise(resolve => web.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { web.close(resolve); web.closeAllConnections(); }));
  const url = `http://127.0.0.1:${web.address().port}/johnason/local-models/models`;
  const rejected = await fetch(url, { headers: { origin: 'http://evil.test' } });
  assert.equal(rejected.status, 403);
  assert.equal(upstream.requests.length, 0);
  const accepted = await fetch(url);
  assert.equal(accepted.status, 200);
  assert.deepEqual((await accepted.json()).models, [{ id: 'worker' }]);
  assert.equal(accepted.headers.get('cache-control'), 'no-store');
});


test('routing cancellation is propagated to the host instead of treated as a model decision', ready, async t => {
  const f = await server(t, () => {});
  const controller = new AbortController();
  const service = make({ baseURL: f.baseURL });
  const pending = service.route({ task: 'hi' }, { ...execution(), signal: controller.signal });
  while (f.requests.length === 0) await new Promise(resolve => setTimeout(resolve, 1));
  controller.abort();
  await assert.rejects(pending, code('LOCAL_MODEL_CANCELLED'));
  assert.equal(f.requests.length, 1);
});

test('disposal cancels active work and rejects any subsequent inference', ready, async t => {
  const f = await server(t, () => {});
  const service = make({ baseURL: f.baseURL });
  const pending = service.generate({ task: 'hi' }, execution());
  while (f.requests.length === 0) await new Promise(resolve => setTimeout(resolve, 1));
  service.dispose();
  await assert.rejects(pending, code('LOCAL_MODEL_DISABLED'));
  await assert.rejects(service.generate({ task: 'again' }, execution()), code('LOCAL_MODEL_DISABLED'));
  assert.equal(f.requests.length, 1);
});

test('registered tool executors use the same task and cancellation boundary', ready, async t => {
  const f = await server(t, (_, res) => reply(res, completion('from tool')));
  const service = make({ baseURL: f.baseURL });
  const tools = createLocalModelTools(service);
  const generate = tools.find(tool => tool.name === 'local_model_generate');
  const result = await generate.execute({ task: 'hello' }, execution());
  assert.equal(result.text, 'from tool');
  assert.equal(generate.output.render({}, result)[0].text, JSON.stringify(result));
});

test('missing or broken credential resolution never reaches the endpoint or exposes provider errors', ready, async t => {
  const f = await server(t, (_, res) => reply(res, completion('should not happen')));
  const secret = randomBytes(20).toString('hex');
  for (const resolveCredential of [async () => undefined, async () => { throw new Error(secret); }]) {
    await assert.rejects(make({ baseURL: f.baseURL, apiKeyRef: 'SYNTHETIC_REF' }, { resolveCredential }).generate({ task: 'hi' }, execution()), code('LOCAL_MODEL_CREDENTIAL_UNAVAILABLE'));
  }
  assert.equal(f.requests.length, 0);
});

test('successful model content cannot reflect the resolved credential', ready, async t => {
  const secret = randomBytes(20).toString('hex');
  const f = await server(t, (_, res) => reply(res, completion(`echo ${secret}`)));
  await assert.rejects(make({ baseURL: f.baseURL, apiKeyRef: 'SYNTHETIC_REF' }, { resolveCredential: async () => ({ value: secret }) }).generate({ task: 'hi' }, execution()), code('LOCAL_MODEL_INVALID_RESPONSE'));
});

test('a cancelled credential lookup retains its concurrency slot until the host provider settles', ready, async t => {
  const f = await server(t, (_, res) => reply(res, completion('next')));
  let release;
  let credentialStarted;
  const started = new Promise(resolve => { credentialStarted = resolve; });
  let first = true;
  const service = make({ baseURL: f.baseURL, apiKeyRef: 'SYNTHETIC_REF', maxConcurrent: 1 }, {
    resolveCredential: () => {
      if (!first) return Promise.resolve({ value: randomBytes(20).toString('hex') });
      first = false;
      credentialStarted();
      return new Promise(resolve => { release = resolve; });
    },
  });
  const controller = new AbortController();
  const pending = service.generate({ task: 'hi' }, { ...execution(), signal: controller.signal });
  await started;
  controller.abort();
  await assert.rejects(pending, code('LOCAL_MODEL_CANCELLED'));
  await assert.rejects(service.generate({ task: 'again' }, execution()), code('LOCAL_MODEL_BUSY'));
  assert.equal(f.requests.length, 0);
  release({ value: randomBytes(20).toString('hex') });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await service.generate({ task: 'after settle' }, execution())).text, 'next');
});

test('plugin disposal aborts routing instead of returning a successful main decision', ready, async t => {
  const f = await server(t, () => {});
  const service = make({ baseURL: f.baseURL });
  const pending = service.route({ task: 'hi' }, execution());
  while (f.requests.length === 0) await new Promise(resolve => setTimeout(resolve, 1));
  service.dispose();
  await assert.rejects(pending, code('LOCAL_MODEL_DISABLED'));
  assert.equal(f.requests.length, 1);
});

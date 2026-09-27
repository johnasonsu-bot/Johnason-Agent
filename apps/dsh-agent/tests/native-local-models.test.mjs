import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { authenticateWeb, redactLaunchOutput } from './fixtures/native-web-client.mjs';

// The real native supervisor loads the bundled Skill and calls the real local
// routing tool. Only the model HTTP responses are scripted, on loopback.
test('bundled Skill drives a Jev-style decision and local worker inside a native agent turn', { timeout: 90000 }, async t => {
  const requests = [];
  const models = createServer(async (req, res) => {
    if (req.method === 'GET' && req.url === '/v1/models') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ data: ['fixture-decision', 'fixture-worker', 'fixture-supervisor'].map(id => ({ id, object: 'model' })) }));
      return;
    }
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    requests.push(body);
    if (body.model !== 'fixture-supervisor') {
      res.writeHead(200, { 'content-type': 'application/json' });
      const content = body.model === 'fixture-decision' ? JSON.stringify({ route: 'local', confidence: 0.95 }) : 'LOCAL_WORKER_RESULT: blue, red';
      res.end(JSON.stringify({ choices: [{ message: { role: 'assistant', content }, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 5 } }));
      return;
    }
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    const base = { id: randomUUID(), object: 'chat.completion.chunk', created: 1, model: body.model };
    const priorTools = body.messages.filter(message => message.role === 'tool');
    let delta, finish;
    if (!body.tools?.length) { delta = { content: 'Fixture title' }; finish = 'stop'; }
    else if (!priorTools.length) {
      delta = { tool_calls: [{ index: 0, id: 'fixture-skill', type: 'function', function: { name: 'skill', arguments: JSON.stringify({ name: 'johnason-local-models' }) } }] };
      finish = 'tool_calls';
    } else if (priorTools.length === 1) {
      delta = { tool_calls: [{ index: 0, id: 'fixture-route', type: 'function', function: { name: 'jev_route_task', arguments: JSON.stringify({ task: 'Extract the colors from blue and red.', executeLocal: true }) } }] };
      finish = 'tool_calls';
    } else if (priorTools.length === 2) {
      delta = { tool_calls: [{ index: 0, id: 'fixture-read', type: 'function', function: { name: 'read', arguments: JSON.stringify({ file_path: 'fixture.txt' }) } }] };
      finish = 'tool_calls';
    } else { delta = { content: 'HYBRID_FLOW_OK' }; finish = 'stop'; }
    res.end([
      { ...base, choices: [{ index: 0, delta: { role: 'assistant', ...delta }, finish_reason: null }] },
      { ...base, choices: [{ index: 0, delta: {}, finish_reason: finish }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } },
      '[DONE]',
    ].map(value => `data: ${typeof value === 'string' ? value : JSON.stringify(value)}\n\n`).join(''));
  });
  await new Promise(resolve => models.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { models.close(resolve); models.closeAllConnections(); }));
  const baseURL = `http://127.0.0.1:${models.address().port}/v1`;
  const root = await mkdtemp(join(tmpdir(), 'dsh-native-hybrid-'));
  t.diagnostic(`Native hybrid fixture retained: ${root}`);
  const workspace = await mkdtemp(join(tmpdir(), 'dsh-hybrid-workspace-'));
  await writeFile(join(workspace, 'fixture.txt'), 'NATIVE_READ_OK\n');
  const probe = createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
  const child = spawn(process.execPath, [cli, 'web', '--data-dir', root, '--port', String(port), '--no-open'], {
    env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', data => { output += data; });
  child.stderr.on('data', data => { output += data; });
  const exited = new Promise(resolve => child.once('exit', resolve));
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM'); await exited; });
  const deadline = Date.now() + 30000;
  while (!output.includes(`dsh web: ${origin}`)) {
    assert.equal(child.exitCode, null, redactLaunchOutput(output));
    assert.ok(Date.now() < deadline, redactLaunchOutput(output));
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  const client = await authenticateWeb(output, origin);
  const { chromium } = await import('playwright-core');
  const browser = await chromium.launch({ headless: true, executablePath: process.env.DSH_TEST_BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  t.after(() => browser.close());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await client.authenticateBrowser(context);
  const external = [];
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin !== origin) { external.push(route.request().url()); await route.abort(); }
    else await route.continue();
  });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin);
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await page.getByText('内测声明', { exact: true }).waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: '插件', exact: true }).click();
  const card = page.locator('[data-plugin-package="@johnason/dsh-local-models"]');
  await card.getByText('local-models', { exact: true }).click();
  const panel = page.locator('[data-johnason-local-models]');
  await panel.getByLabel('LM Studio API 地址', { exact: true }).fill(baseURL);
  await panel.getByLabel('Jev 决策模型', { exact: true }).fill('fixture-decision');
  await panel.getByLabel('本地工作模型', { exact: true }).fill('fixture-worker');
  await panel.getByRole('button', { name: '保存混合配置', exact: true }).click();
  await panel.getByRole('status').filter({ hasText: '混合配置已保存。' }).waitFor();
  assert.equal(requests.length, 0, 'saving configuration must never run inference');
  assert.deepEqual(await (await client.fetch('/vault/status')).json(), { initialized: false, locked: true });
  const discoveredResponse = page.waitForResponse(response => response.url() === `${origin}/johnason/local-models/models`);
  await panel.getByRole('button', { name: '读取本地模型', exact: true }).click();
  const discovered = await discoveredResponse;
  assert.equal(discovered.status(), 200, await discovered.text());
  await panel.locator('li').filter({ hasText: 'fixture-decision' }).waitFor();
  await panel.getByRole('heading', { name: '本地模型与 Jev 混合调用' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(root, 'local-models.png'), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  const password = randomUUID(), key = randomUUID();
  const initialized = await client.fetch('/vault/initialize', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password, confirmation: password }) });
  assert.equal(initialized.status, 200);
  await client.rpc('credentials/set', { ref: 'FIXTURE_MAIN', value: key });
  const settings = await client.rpc('settings/describe', {});
  const pi = settings.namespaces.find(row => row.ns === 'llm-pi-ai');
  await client.rpc('settings/mutate', { ns: pi.ns, expectedRevision: pi.revision, ops: [{ op: 'set', path: ['providers', 'fixture-supervisor'], value: {
    api: 'openai-completions', baseURL, apiKeyEnv: 'FIXTURE_MAIN', models: [{ id: 'fixture-supervisor', maxTokens: 512 }],
  } }] });
  const created = await client.rpc('workspace/create', { request: { path: workspace } });
  const { sessionId } = await client.rpc('session/create', { request: { workspaceId: created.workspace.workspaceId } });
  await client.rpc('session/selectModel', { request: { sessionId, provider: 'fixture-supervisor', model: 'fixture-supervisor' } });
  await client.rpc('session/prompt', { request: { requestId: randomUUID(), sessionId, mode: 'queue', content: [{ type: 'text', text: 'Use the johnason-local-models Skill to extract colors from blue and red through the local worker.' }] } });
  let history;
  const finishBy = Date.now() + 25000;
  while (true) {
    history = await client.history(sessionId);
    const turnError = history.records.find(row => row.event?.type === 'turn/end' && row.event.data.reason?.kind === 'error');
    assert.equal(turnError, undefined, JSON.stringify(turnError?.event?.data));
    if (history.records.some(row => row.event?.type === 'assistant/message' && row.event.data.message.content.some(block => block.type === 'text' && block.text.includes('HYBRID_FLOW_OK')))) break;
    assert.ok(Date.now() < finishBy, `Native Skill and hybrid execution must finish: ${JSON.stringify({ events: history.records.map(row => ({ type: row.event?.type, ...(/tool|error|failure|end/.test(row.event?.type) ? { data: row.event?.data } : {}) })), models: requests.map(row => row.model), output: redactLaunchOutput(output) })}`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(JSON.stringify(history).includes('LOCAL_WORKER_RESULT'), 'worker output must return into the supervisor context');
  assert.ok(JSON.stringify(history).includes('johnason-local-models'), 'native skill must be loaded');
  assert.ok(JSON.stringify(history).includes('NATIVE_READ_OK'), 'a regular native tool must use the same scheduler');
  assert.equal(requests.filter(row => row.model === 'fixture-decision').length, 1);
  assert.equal(requests.filter(row => row.model === 'fixture-worker').length, 1);
  const decision = requests.find(row => row.model === 'fixture-decision');
  assert.equal(decision.response_format.type, 'json_schema');
  assert.equal(decision.stream, false);
  assert.equal(decision.messages.some(message => message.content.includes('Use the johnason-local-models Skill')), false, 'only explicit subtask text may reach the decision model');
  assert.equal((await client.snapshot(sessionId)).projections.values.modelSelection.next.provider, 'fixture-supervisor');
  assert.equal(output.includes(key), false);
  t.diagnostic(`Native Skill / hybrid browser evidence retained: ${root}`);
});

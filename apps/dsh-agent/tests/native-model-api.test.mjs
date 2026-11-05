import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { authenticateWeb, redactLaunchOutput } from './fixtures/native-web-client.mjs';

// Real UI -> authenticated native settings/credentials -> encrypted disk.
// Fails if the bundle is merely copied but not usable, or if saving replaces
// other provider routes, leaks a key into settings, or omits native registration.
test('model API plugin saves local and cloud connections without overwriting existing models or exposing keys', { timeout: 90000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-model-api-'));
  const workspace = await mkdtemp(join(tmpdir(), 'dsh-model-api-workspace-'));
  const requests = [];
  const model = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    requests.push({ path: req.url, authorized: Boolean(req.headers.authorization), body: JSON.parse(Buffer.concat(chunks).toString()) });
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    const frame = { id: 'local-fixture', object: 'chat.completion.chunk', created: 1, model: 'local-test-model' };
    res.end([
      { ...frame, choices: [{ index: 0, delta: { role: 'assistant', content: 'MODEL_API_CONNECTED' }, finish_reason: null }] },
      { ...frame, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 } },
      '[DONE]',
    ].map(value => `data: ${typeof value === 'string' ? value : JSON.stringify(value)}\n\n`).join(''));
  });
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { model.close(resolve); model.closeAllConnections(); }));
  const modelURL = `http://127.0.0.1:${model.address().port}/v1`;
  const probe = createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
  let child, output = '', exited;
  async function start() {
    output = '';
    child = spawn(process.execPath, [cli, 'web', '--data-dir', root, '--port', String(port), '--no-open'], {
      env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout.on('data', data => { output += data; });
    child.stderr.on('data', data => { output += data; });
    exited = new Promise(resolve => child.once('exit', resolve));
    const deadline = Date.now() + 30000;
    while (!output.includes(`dsh web: ${origin}`)) {
      assert.equal(child.exitCode, null, redactLaunchOutput(output));
      assert.ok(Date.now() < deadline, redactLaunchOutput(output));
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return authenticateWeb(output, origin);
  }
  async function stop() {
    if (child?.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    await exited;
  }
  t.after(stop);
  let client = await start();
  const namespace = async () => (await client.rpc('settings/describe', {})).namespaces.find(row => row.ns === 'llm-pi-ai');
  let initial = await namespace();
  const existing = { api: 'openai-completions', baseURL: 'http://127.0.0.1:1234/v1', models: [{ id: 'preserved-model', maxTokens: 512 }] };
  await client.rpc('settings/mutate', { ns: 'llm-pi-ai', ops: [{ op: 'set', path: ['providers', 'preserved-test'], value: existing }], expectedRevision: initial.revision });
  const preserved = (await namespace()).value.providers['preserved-test'];
  const { chromium } = await import('playwright-core');
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.DSH_TEST_BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  t.after(() => browser.close());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await client.authenticateBrowser(context);
  const external = [];
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin !== origin) { external.push(route.request().url()); await route.abort(); }
    else await route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin);
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await page.getByText('内测声明', { exact: true }).waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: '插件', exact: true }).click();
  const card = page.locator('[data-plugin-package="@johnason/dsh-model-api"]');
  await card.waitFor();
  await card.getByRole('button', { name: /^查看 / }).click();
  const panel = page.locator('[data-johnason-model-api]');
  await panel.waitFor();
  await panel.getByLabel('连接 ID', { exact: true }).fill('local-test');
  await panel.getByLabel('显示名称', { exact: true }).fill('本地测试模型');
  await panel.getByLabel('API 地址', { exact: true }).fill(modelURL);
  await panel.getByLabel('模型 ID', { exact: true }).fill('local-test-model\nlocal-second-model');
  // First attempt must refuse a locked/uninitialized Vault without a partial provider write.
  await panel.getByRole('button', { name: '保存连接', exact: true }).click();
  await panel.getByRole('alert').waitFor();
  assert.equal((await namespace()).value.providers['johnason-local-test'], undefined);
  const password = randomUUID();
  const initialize = await client.fetch('/vault/initialize', { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password, confirmation: password }) });
  assert.equal(initialize.status, 200);
  await panel.getByRole('button', { name: '刷新列表', exact: true }).click();
  await panel.getByRole('button', { name: '保存连接', exact: true }).click();
  await panel.getByText('johnason-local-test', { exact: true }).waitFor();
  const local = (await namespace()).value.providers['johnason-local-test'];
  assert.equal(local.baseURL, modelURL);
  assert.equal(local.api, 'openai-completions');
  assert.equal(local.models[0].id, 'local-test-model');
  assert.deepEqual(local.models.map(model => model.id), ['local-test-model', 'local-second-model']);
  assert.equal((await client.rpc('credentials/describe', { refs: [local.apiKeyEnv] }))[local.apiKeyEnv].configured, true);
  await panel.getByLabel('添加模型 ID（johnason-local-test）', { exact: true }).fill('local-third-model');
  await panel.getByRole('button', { name: '添加模型（johnason-local-test）', exact: true }).click();
  await panel.getByText(/local-third-model/).waitFor();
  const expanded = (await namespace()).value.providers['johnason-local-test'];
  assert.deepEqual(expanded.models.slice(0, 2), local.models, 'appending must preserve existing model parameters');
  assert.equal(expanded.models[2].id, 'local-third-model');
  assert.equal(expanded.apiKeyEnv, local.apiKeyEnv);
  assert.equal(expanded.baseURL, local.baseURL);
  await panel.getByLabel('连接类型', { exact: true }).selectOption('cloud');
  await panel.getByLabel('连接 ID', { exact: true }).fill('cloud-test');
  await panel.getByLabel('显示名称', { exact: true }).fill('外部测试模型');
  await panel.getByLabel('API 地址', { exact: true }).fill('https://example.invalid/v1');
  await panel.getByLabel('模型 ID', { exact: true }).fill('cloud-test-model');
  const key = randomUUID();
  await panel.getByLabel('API Key', { exact: true }).fill(key);
  await panel.getByRole('button', { name: '保存连接', exact: true }).click();
  await panel.getByText('johnason-cloud-test', { exact: true }).waitFor();
  assert.equal(await panel.getByLabel('API Key', { exact: true }).inputValue(), '');
  assert.equal((await panel.innerText()).includes(key), false);
  const saved = await namespace();
  const cloud = saved.value.providers['johnason-cloud-test'];
  assert.equal(cloud.models[0].id, 'cloud-test-model');
  assert.notEqual(local.apiKeyEnv, cloud.apiKeyEnv);
  assert.deepEqual(saved.value.providers['preserved-test'], preserved);
  assert.equal(JSON.stringify(saved).includes(key), false);
  assert.equal((await readFile(join(root, 'profiles/web/cordis.patch.yml'), 'utf8')).includes(key), false);
  assert.equal((await readFile(join(root, 'vault.enc'), 'utf8')).includes(key), false);
  assert.equal(output.includes(key), false);
  const created = await client.rpc('workspace/create', { request: { path: workspace } });
  const { sessionId } = await client.rpc('session/create', { request: { workspaceId: created.workspace.workspaceId } });
  const selection = await client.rpc('session/selectModel', { request: { sessionId, provider: 'johnason-local-test', model: 'local-test-model' } });
  assert.equal(selection.selected.provider, 'johnason-local-test');
  assert.deepEqual(await client.rpc('session/prompt', { request: { requestId: randomUUID(), sessionId, mode: 'queue',
    content: [{ type: 'text', text: 'Return the connection test marker.' }] } }), { accepted: true });
  const completedBy = Date.now() + 20000;
  while (true) {
    const history = await client.history(sessionId);
    if (history.records.some(row => row.event?.type === 'assistant/message'
      && row.event.data.message.content.some(block => block.type === 'text' && block.text.includes('MODEL_API_CONNECTED')))) break;
    assert.ok(Date.now() < completedBy, 'Configured local model must return through the native agent');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  // Native session plugins may also request a title from the selected model.
  assert.ok(requests.length >= 1);
  for (const request of requests) {
    assert.equal(request.path, '/v1/chat/completions');
    assert.equal(request.authorized, true);
    assert.equal(request.body.model, 'local-test-model');
  }
  await page.screenshot({ path: join(root, 'model-api.png'), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  await browser.close();
  await stop();
  client = await start();
  const restarted = await namespace();
  assert.deepEqual(restarted.value.providers, saved.value.providers);
  assert.deepEqual(await (await client.fetch('/vault/status')).json(), { initialized: true, locked: true });
  t.diagnostic(`Model API plugin evidence retained: ${root}`);
});

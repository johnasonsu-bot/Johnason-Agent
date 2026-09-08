import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// Real headless Chromium navigation/reload, never an API-only reconnect substitute.
// A bundled playwright-core may be selected explicitly; normal installs use devDependencies.
test('browser reload restores the selected native session, title, model and idle state', { timeout: 80000 }, async t => {
  const { chromium } = await import(process.env.DSH_TEST_PLAYWRIGHT_PATH
    ? pathToFileURL(process.env.DSH_TEST_PLAYWRIGHT_PATH).href : 'playwright-core');
  const root = await mkdtemp(join(tmpdir(), 'dsh-browser-refresh-'));
  const workspace = await mkdtemp(join(tmpdir(), 'dsh-browser-workspace-'));
  const probe = createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const child = spawn(process.execPath, [fileURLToPath(new URL('../src/cli.mjs', import.meta.url)), 'web', '--data-dir', root, '--port', String(port), '--no-open'], {
    env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', bytes => { output += bytes; });
  child.stderr.on('data', bytes => { output += bytes; });
  const exit = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM'); await exit; });
  const deadline = Date.now() + 30000;
  while (!output.includes(`dsh web: ${url}`)) {
    assert.equal(child.exitCode, null, output);
    assert.ok(Date.now() < deadline, `Web startup timed out: ${output}`);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  async function rpc(method, payload) {
    const response = await fetch(`${url}/api/${method}`, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' }, body: JSON.stringify({ type: 'client-request', rpcId: 'browser-fixture', method, payload }) });
    const body = await response.json();
    assert.equal(body.result.ok, true, JSON.stringify(body));
    return body.result.value;
  }
  // Setup through public native APIs only. No model request or credential is needed.
  const created = await rpc('workspace.create', { path: workspace });
  const { sessionId } = await rpc('session.create', { workspaceId: created.workspace.workspaceId });
  const title = 'Browser refresh acceptance';
  await rpc('session.rename', { sessionId, title });
  await rpc('session.selectModel', { sessionId, provider: 'deepseek-official', model: 'deepseek-v4-pro' });
  const beforeModels = await rpc('session.models', { sessionId });
  const beforeHistory = await rpc('session.history', { sessionId });
  const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const executablePath = process.env.DSH_TEST_BROWSER_PATH || (existsSync(chrome) ? chrome : undefined);
  const browser = await chromium.launch({ headless: true, executablePath });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  // Block every external HTTP request, including any accidental model/telemetry request.
  const external = [];
  await page.route('**/*', async route => {
    if (!route.request().url().startsWith(url + '/')) { external.push(route.request().url()); await route.abort(); }
    else await route.continue();
  });
  const clientRequests = [];
  page.on('request', request => {
    if (request.url().includes('/api/')) {
      try { clientRequests.push(request.postDataJSON()); } catch {}
    }
  });
  await page.goto(url);
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await page.getByText('内测声明', { exact: true }).waitFor({ state: 'hidden' });
  await page.getByText('DeepSeek-V4-Pro', { exact: true }).waitFor();
  assert.ok(clientRequests.some(request => request?.payload?.sessionId === sessionId), JSON.stringify(clientRequests));
  const selectedUrl = page.url();
  await page.screenshot({ path: join(root, 'before-refresh.png'), fullPage: true });
  clientRequests.length = 0;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByText('DeepSeek-V4-Pro', { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => performance.getEntriesByType('navigation')[0].type), 'reload');
  assert.equal(page.url(), selectedUrl);
  assert.ok(clientRequests.some(request => request?.payload?.sessionId === sessionId), JSON.stringify(clientRequests));
  // Native UI deliberately calls an empty session "New Session", even if renamed.
  // Its title remains verified through history; selected identity is read from the page client's requests.
  assert.deepEqual((await rpc('session.models', { sessionId })).current, beforeModels.current);
  assert.deepEqual(await rpc('session.history', { sessionId }), beforeHistory);
  const sessions = await rpc('session.list', {});
  assert.equal(sessions.items.length, 1);
  assert.equal(sessions.items[0].sessionId, sessionId);
  assert.equal(sessions.items[0].running, false);
  assert.equal(await page.getByRole('button', { name: '停止生成', exact: true }).count(), 0);
  assert.deepEqual(external, []);
  await page.screenshot({ path: join(root, 'after-refresh.png'), fullPage: true });
  await browser.close();
  child.kill('SIGTERM');
  assert.deepEqual(await exit, { code: 0, signal: null });
  t.diagnostic(`browser evidence: ${root}; session: ${sessionId}; Chromium ${browser.version()}`);
});

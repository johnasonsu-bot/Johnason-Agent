import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { apply } from '../browser/index.mjs';

function registeredHandler(providers = []) {
  const routes = [];
  const ctx = {
    webServer: { register(route) { routes.push(route); return () => {}; } },
    effect(run) { return run(); },
    inject(services, run) { assert.deepEqual(services, ['webServer']); return run(ctx); },
  };
  apply(ctx, { providers });
  assert.equal(routes.length, 1, 'The local plugin must register its status endpoint');
  assert.equal(routes[0].kind, 'exact');
  assert.equal(routes[0].path, '/johnason/plugins');
  return routes[0].handler;
}

async function serve(t, handler) {
  const server = createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  return `http://127.0.0.1:${server.address().port}`;
}

test('local plugin metadata exposes only safe provider fields and has no-store responses', async t => {
  const handler = registeredHandler([{ id: 'forge', label: 'Forge DevOps', type: 'forge', token: 'secret-token', url: '/secret-path', config: { password: 'secret-password' } }]);
  const url = await serve(t, handler);
  const response = await fetch(url + '/johnason/plugins');
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const body = await response.json();
  assert.equal(body.schemaVersion, 1);
  assert.equal(body.pluginId, '@johnason/dsh-memory-ui');
  assert.deepEqual(body.providers, [{ id: 'forge', label: 'Forge DevOps', type: 'forge' }]);
  assert.ok(body.features.includes('memory-recovery'));
  assert.doesNotMatch(JSON.stringify(body), /secret|token|password|config/);
});

test('local plugin metadata rejects writes, hostile origins, DNS rebinding and nonlocal peers', async t => {
  const handler = registeredHandler();
  const url = await serve(t, handler);
  for (const method of ['POST', 'PUT', 'DELETE', 'HEAD']) assert.equal((await fetch(url + '/johnason/plugins', { method })).status, 405);
  for (const headers of [{ origin: 'https://evil.example' }, { host: 'evil.example' }, { 'sec-fetch-site': 'cross-site' }]) {
    const status = await new Promise((resolve, reject) => {
      const req = request(url + '/johnason/plugins', { headers }, res => { res.resume(); res.once('end', () => resolve(res.statusCode)); });
      req.on('error', reject); req.end();
    });
    assert.equal(status, 403, JSON.stringify(headers));
  }
  let status;
  await handler({ method: 'GET', url: '/johnason/plugins', headers: { host: '127.0.0.1:1234' }, socket: { localPort: 1234, remoteAddress: '192.0.2.1' } }, { writeHead(value) { status = value; }, end() {} });
  assert.equal(status, 403);
});

test('native config slot renders functional local cards, refreshes state, and separates failures from missing configuration', { timeout: 30000 }, async t => {
  // Render the shipped factory with real React; all requests terminate at this test server.
  const requireNative = createRequire(new URL('../../../third_party/deepseek-harness/packages/client/ui-plugin-manager/package.json', import.meta.url));
  const react = await readFile(join(dirname(requireNative.resolve('react')), 'umd/react.development.js'), 'utf8');
  const reactDom = await readFile(join(dirname(requireNative.resolve('react-dom')), 'umd/react-dom.development.js'), 'utf8');
  const client = await readFile(new URL('../browser/client.js', import.meta.url), 'utf8');
  let stage = 'empty';
  const requests = [];
  const url = await serve(t, (req, res) => {
    requests.push({ path: req.url, method: req.method });
    const send = (status, body) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (req.url === '/') { res.writeHead(200, { 'content-type': 'text/html' }); return res.end('<!doctype html><div id="root"></div>'); }
    if (stage === 'error') return send(503, { error: 'secret-server-details' });
    if (req.url === '/johnason/plugins') return send(200, { schemaVersion: 1, providers: stage === 'empty' ? [] : [{ id: 'forge', label: 'Forge DevOps', type: 'forge' }, { id: 'data', label: 'Data Platform', type: 'dataplatform' }] });
    if (req.url === '/vault/status') return send(200, { initialized: stage !== 'empty', locked: stage !== 'ready' });
    if (req.url === '/memory-recovery/api/sessions') return send(200, { value: stage === 'empty' ? [] : [{ id: 'private-session-id', cwd: '/private/workspace', config: { enabled: true, sandbox: { mode: 'read-only' } }, status: { content: 'private-memory-content' } }] });
    return send(404, { error: 'unknown' });
  });
  const { chromium } = await import('playwright-core');
  const browser = await chromium.launch({ headless: true, executablePath: process.env.DSH_TEST_BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.goto(url);
  await page.addScriptTag({ content: react });
  await page.addScriptTag({ content: reactDom });
  await page.evaluate(() => {
    window.registrations = [];
    window.__ModuleLoader__ = { load(module) { window.plugin = module.factory(id => { if (id === 'react') return window.React; throw new Error(id); }); } };
  });
  await page.addScriptTag({ content: client });
  await page.evaluate(() => {
    window.plugin.apply({ slots: { inject(name, install) { return install(); }, register(options, render) { window.registrations.push({ options, render }); } } });
  });
  const slots = await page.evaluate(() => window.registrations.map(row => row.options));
  assert.ok(slots.some(row => row.name === 'conversation.input.dock'), 'Existing memory dock remains registered');
  assert.ok(slots.some(row => row.name === 'sidebar.brand.name'), 'Existing brand remains registered');
  assert.ok(slots.some(row => row.name === 'plugins.bundle.config' && row.key === '@johnason/dsh-memory-ui'), 'Native plugin page configuration must be registered');
  await page.evaluate(() => {
    window.panel = window.registrations.find(row => row.options.name === 'plugins.bundle.config');
    window.panelRoot = window.ReactDOM.createRoot(document.getElementById('root'));
    window.panelRoot.render(window.panel.render({ view: 'summary' }));
  });
  await page.getByText(/Vault.*三类记忆/).waitFor();
  assert.equal(requests.filter(row => !['/', '/favicon.ico'].includes(row.path)).length, 0, 'Summary does not poll services');
  await page.evaluate(() => window.panelRoot.render(window.panel.render({ view: 'page' })));
  await page.getByRole('heading', { name: 'Johnason 本地功能' }).waitFor();
  await page.getByText('未初始化', { exact: true }).waitFor();
  await page.getByText('未配置', { exact: true }).first().waitFor();
  assert.match(await page.locator('body').innerText(), /按会话显式启用/);
  assert.match(await page.locator('body').innerText(), /停用.*入口.*不会.*会话约束/);
  assert.equal(await page.locator('a[href="/vault"]').count(), 1);
  assert.ok(await page.locator('a[href="/memory-recovery"]').count() >= 3);
  assert.equal(await page.locator('a[href="/systems"]').count(), 0);
  stage = 'ready';
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await page.getByText('已解锁', { exact: true }).waitFor();
  await page.getByText('已配置', { exact: true }).first().waitFor();
  assert.ok(await page.locator('a[href="/systems"]').count() >= 1);
  assert.match(await page.locator('body').innerText(), /1 个会话已启用/);
  assert.doesNotMatch(await page.locator('body').innerText(), /private-session-id|private\/workspace|private-memory-content/);
  stage = 'error';
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await page.getByText('服务不可用', { exact: true }).first().waitFor();
  assert.equal(await page.getByText('未配置', { exact: true }).count(), 0);
  assert.doesNotMatch(await page.locator('body').innerText(), /secret-server-details/);
  assert.ok(requests.some(row => row.path === '/memory-recovery/api/sessions' && row.method === 'POST'));
  assert.ok(requests.every(row => ['/', '/johnason/plugins', '/vault/status', '/memory-recovery/api/sessions', '/favicon.ico'].includes(row.path)), 'No model calls, logins or business commands occur');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { authenticateWeb, redactLaunchOutput } from './fixtures/native-web-client.mjs';

// Catch a bundle that is present on disk but absent from Installed, or a config
// slot that never activates in the actual native manager. No external requests.
test('native Installed plugin opens all local feature controls and reports unconfigured systems honestly', { timeout: 60000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-local-plugins-'));
  const probe = createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  const child = spawn(process.execPath, [fileURLToPath(new URL('../src/cli.mjs', import.meta.url)), 'web', '--data-dir', root, '--port', String(port), '--no-open'], {
    env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', data => { output += data; });
  child.stderr.on('data', data => { output += data; });
  const exited = new Promise(resolve => child.once('exit', resolve));
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    await exited;
  });
  const deadline = Date.now() + 30000;
  while (!output.includes(`dsh web: ${origin}`)) {
    assert.equal(child.exitCode, null, redactLaunchOutput(output));
    assert.ok(Date.now() < deadline, redactLaunchOutput(output));
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  const client = await authenticateWeb(output, origin);
  const { chromium } = await import('playwright-core');
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.DSH_TEST_BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  t.after(() => browser.close());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
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
  const card = page.locator('[data-plugin-package="@johnason/dsh-memory-ui"]');
  await card.waitFor();
  assert.match(await card.innerText(), /Vault.*三类记忆.*恢复审计.*沙箱/);
  await card.getByRole('button', { name: /^查看 / }).click();
  const panel = page.locator('[data-johnason-plugins]');
  await panel.waitFor();
  await panel.getByText(/未初始化/).waitFor();
  await panel.getByText(/未配置/).first().waitFor();
  assert.ok(await panel.locator('a[href="/vault"]').count());
  assert.ok(await panel.locator('a[href="/memory-recovery"]').count());
  assert.equal(await panel.locator('a[href="/systems"]').count(), 0, 'unconfigured service must not have a broken launch link');
  assert.match(await panel.innerText(), /Forge/);
  assert.match(await panel.innerText(), /Data Platform/);
  const metadata = await client.fetch('/johnason/plugins');
  assert.equal(metadata.status, 200);
  assert.deepEqual((await metadata.json()).providers, []);
  for (const route of ['/vault', '/memory-recovery']) assert.equal((await client.fetch(route)).status, 200);
  await page.screenshot({ path: join(root, 'local-plugins.png'), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  t.diagnostic(`Local plugin browser evidence retained: ${root}`);
});

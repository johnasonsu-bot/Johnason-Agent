#!/usr/bin/env node
// Opt-in smoke for a previously provisioned, disposable ecosystem profile.
// Authentication stays in memory; the evidence contains no credentials.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdir, mkdtemp, readFile, realpath, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { isAbsolute, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import { authenticateWeb, redactLaunchOutput } from '../tests/fixtures/native-web-client.mjs';
import { COMMUNITY_PLUGINS } from '../src/ecosystem.mjs';

const args = process.argv.slice(2);
const options = {};
for (let i = 0; i < args.length; i += 2) {
  assert.ok(['--data-dir', '--workspace'].includes(args[i]) && args[i + 1], 'Explicit --data-dir and --workspace are required');
  options[args[i]] = args[i + 1];
}
const temporaryRoots = await Promise.all(['/private/tmp', tmpdir()].map(path => realpath(path)));
async function isolatedPath(value) {
  assert.ok(typeof value === 'string' && isAbsolute(value), 'Explicit absolute test paths are required');
  const path = await realpath(value);
  assert.ok(temporaryRoots.some(root => {
    const child = relative(root, path);
    return child && !child.startsWith('..') && !isAbsolute(child);
  }), 'Only existing isolated temporary directories may be verified');
  return path;
}
const dataRoot = await isolatedPath(options['--data-dir']);
const workspace = await isolatedPath(options['--workspace']);
assert.notEqual(dataRoot, workspace, 'Use distinct test profile and workspace directories');
assert.equal(JSON.parse(await readFile(join(dataRoot, 'johnason-ecosystem.json'), 'utf8')).schema, 'johnason.ecosystem.v1');
const patch = join(dataRoot, 'johnason-ecosystem.patch.json');
const overlay = JSON.parse(await readFile(patch, 'utf8'));
const disabled = ['web-ui-remote-bridge', 'web-ui-task-board-remote', 'web-ui-ssh', 'web-ui-agents', 'web-ui-market',
  'deepseek-account', 'account-controller', 'ui-account-notifications', 'ui-settings-account'];
for (const id of disabled) assert.equal(overlay.find(row => row.id === id)?.disabled, true, `${id} must remain disabled`);
assert.deepEqual(overlay.find(row => row.id === 'llm-subscriptions')?.config.providers, ['codex']);
const packages = [];
for (const plugin of COMMUNITY_PLUGINS) {
  const installed = JSON.parse(await readFile(join(dataRoot, 'profiles/web/node_modules', plugin.name, 'package.json'), 'utf8'));
  assert.equal(installed.version, plugin.version, `Pinned ${plugin.name} version`);
  packages.push({ name: installed.name, version: installed.version });
}
const evidence = await mkdtemp(join(tmpdir(), 'johnason-ecosystem-live-'));
const runtimeHome = join(evidence, 'home');
await mkdir(runtimeHome, { mode: 0o700 });
const probe = createServer();
await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
const port = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const origin = `http://127.0.0.1:${port}`;
const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
const child = spawn(process.execPath, [cli, 'web', '--data-dir', dataRoot, '--workspace', workspace,
  '--local-ollama', '--port', String(port), '--patch', patch, '--no-open'], {
  env: { PATH: process.env.PATH, HOME: runtimeHome, TMPDIR: process.env.TMPDIR },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let output = '';
child.stdout.on('data', bytes => { output += bytes; });
child.stderr.on('data', bytes => { output += bytes; });
const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
let browser, page;
let stage = 'startup';
const javascriptErrors = [], consoleErrors = [], blockedOrigins = new Set(), failedLocalRequests = [], streamClosures = [], failedLocalResponses = [];
const establishedTaskStreams = new WeakSet();
const receipt = { status: 'pending', isolatedDataRoot: dataRoot, isolatedWorkspace: workspace, evidence,
  packages, disabled, subscriptionsProviders: ['codex'] };
try {
  const deadline = Date.now() + 45000;
  while (!output.includes(`dsh web: ${origin}`)) {
    assert.equal(child.exitCode, null, redactLaunchOutput(output));
    assert.ok(Date.now() < deadline, `Ecosystem startup timed out: ${redactLaunchOutput(output)}`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.equal((await fetch(origin)).status, 401, 'Unauthenticated access remains denied');
  const client = await authenticateWeb(output, origin);
  assert.equal((await client.fetch('/')).status, 200);
  stage = 'browser-load';
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.DSH_TEST_BROWSER_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(20000);
  await client.authenticateBrowser(page.context());
  await page.route('**/*', async route => {
    if (new URL(route.request().url()).origin !== origin) {
      blockedOrigins.add(new URL(route.request().url()).origin);
      await route.abort();
    } else await route.continue();
  });
  page.on('pageerror', error => javascriptErrors.push(redactLaunchOutput(error.message)));
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(redactLaunchOutput(message.text())); });
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.origin === origin && response.status() >= 400) failedLocalResponses.push({ path: url.pathname, status: response.status() });
    if (url.origin === origin && url.pathname === '/api/task-board/events' && response.status() === 200
      && response.headers()['content-type']?.includes('text/event-stream')) establishedTaskStreams.add(response.request());
  });
  page.on('requestfailed', request => {
    const url = new URL(request.url());
    if (url.origin !== origin) return;
    const failure = { path: url.pathname, error: request.failure()?.errorText, resourceType: request.resourceType() };
    // Component teardown and reload intentionally close this long-lived SSE.
    if (failure.path === '/api/task-board/events'
      && failure.error === 'net::ERR_ABORTED' && failure.resourceType === 'eventsource'
      && ['settings', 'reload'].includes(stage) && establishedTaskStreams.has(request)) streamClosures.push({ ...failure, establishedStatus: 200, stage });
    else failedLocalRequests.push(failure);
  });
  await page.goto(origin);
  const continueButton = page.getByRole('button', { name: '继续', exact: true });
  if (await continueButton.isVisible()) await continueButton.click();
  await page.getByRole('button', { name: '插件', exact: true }).waitFor();
  await page.screenshot({ path: join(evidence, 'home.png'), fullPage: true });
  stage = 'settings';
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('button', { name: '订阅', exact: true }).click();
  await page.getByText('Codex (ChatGPT)', { exact: true }).waitFor();
  await page.getByText('未登录', { exact: true }).waitFor();
  assert.equal(await page.getByText('Claude', { exact: true }).count(), 0, 'Only Codex is enabled');
  await page.screenshot({ path: join(evidence, 'subscriptions.png'), fullPage: true });
  await page.getByRole('button', { name: 'Web 扩展', exact: true }).click();
  await page.getByRole('tab', { name: '皮肤', exact: true }).waitFor();
  await page.screenshot({ path: join(evidence, 'web-extension.png'), fullPage: true });
  await page.getByRole('button', { name: '插件市场', exact: true }).click();
  await page.getByText('发现社区为 DeepSeek Harness 开发的插件', { exact: true }).waitFor();
  await page.getByRole('button', { name: /^已安装/ }).click();
  await page.getByText('@linxin666/dsh-web-all', { exact: true }).waitFor();
  await page.getByText('dsh-plugin-subscriptions', { exact: true }).waitFor();
  await page.screenshot({ path: join(evidence, 'market.png'), fullPage: true });
  assert.deepEqual(failedLocalRequests, [], 'No failed local requests before reload');
  stage = 'reload';
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: '插件', exact: true }).waitFor();
  assert.equal(await page.evaluate(() => performance.getEntriesByType('navigation')[0].type), 'reload');
  await page.screenshot({ path: join(evidence, 'after-refresh.png'), fullPage: true });
  assert.deepEqual(javascriptErrors, [], 'No uncaught JavaScript errors');
  assert.deepEqual(consoleErrors, [], 'No browser console errors');
  assert.deepEqual(failedLocalRequests, [], 'No failed local assets or API requests');
  assert.deepEqual(failedLocalResponses, [], 'No local HTTP error responses');
  receipt.status = 'passed';
  receipt.browser = browser.version();
  receipt.streamClosures = streamClosures;
  receipt.javascriptErrors = javascriptErrors;
  receipt.consoleErrors = consoleErrors;
  receipt.failedLocalRequests = failedLocalRequests;
  receipt.failedLocalResponses = failedLocalResponses;
  receipt.verifiedAt = new Date().toISOString();
} catch (error) {
  receipt.status = 'failed';
  receipt.stage = stage;
  receipt.error = redactLaunchOutput(error.message);
  receipt.javascriptErrors = javascriptErrors;
  receipt.consoleErrors = consoleErrors;
  receipt.failedLocalRequests = failedLocalRequests;
  receipt.failedLocalResponses = failedLocalResponses;
  receipt.launchOutput = redactLaunchOutput(output);
  if (page) {
    await page.screenshot({ path: join(evidence, 'failure.png'), fullPage: true }).catch(() => {});
    receipt.visibleText = redactLaunchOutput(await page.locator('body').innerText().catch(() => 'unavailable'));
  }
  process.exitCode = 1;
} finally {
  receipt.blockedOrigins = [...blockedOrigins];
  try {
    if (browser) await browser.close();
  } catch {
    receipt.status = 'failed';
    receipt.cleanupError = 'Browser cleanup failed';
    process.exitCode = 1;
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    receipt.exit = await exited;
  }
  if (receipt.exit.code !== 0 || receipt.exit.signal !== null) {
    receipt.status = 'failed';
    receipt.cleanupError = 'Ecosystem host did not exit normally';
    process.exitCode = 1;
  }
  await writeFile(join(evidence, 'verification.json'), JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
  process.stdout.write(JSON.stringify(receipt) + '\n');
}

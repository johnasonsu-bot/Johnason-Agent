// Read-only restart acceptance for the NEW fixture produced by accept-memory-recovery.mjs.
// No prompt, memory mutation, credential read/unlock, deletion, or effect dispatch.
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
const root = process.argv[2];
assert.match(root ?? '', /^\/Users\/sushi\/dsh-memory-live-[A-Za-z0-9]+$/);
const before = JSON.parse(await readFile(join(root, 'acceptance-report.json'), 'utf8'));
const probe = createServer(); await new Promise(r => probe.listen(0, '127.0.0.1', r));
const port = probe.address().port; await new Promise(r => probe.close(r));
const url = `http://127.0.0.1:${port}`;
const child = spawn(process.execPath, [fileURLToPath(new URL('../../src/cli.mjs', import.meta.url)), 'web', '--data-dir', root, '--workspace', before.workspaceRoot, '--port', String(port), '--patch', join(root, 'local-model.patch.json'), '--no-open'], { env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'] });
let log = ''; for (const stream of [child.stdout, child.stderr]) stream.on('data', b => { log += b; process.stdout.write(b); });
const exit = new Promise(r => child.once('exit', (code, signal) => r({ code, signal })));
let browser;
try {
  const deadline = Date.now() + 30000;
  while (!log.includes(`dsh web: ${url}`)) { assert.equal(child.exitCode, null, log); assert.ok(Date.now() < deadline); await new Promise(r => setTimeout(r, 100)); }
  const post = async (path, data) => { const r = await fetch(url + path, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' }, body: JSON.stringify(data) }); assert.equal(r.status, 200); return r.json(); };
  const rpc = async (method, payload) => { const r = await post('/api/' + method, { type: 'client-request', rpcId: 'reopen-read-only', method, payload }); assert.equal(r.result.ok, true, JSON.stringify(r)); return r.result.value; };
  const api = async (action, data = {}) => (await post('/memory-recovery/api/' + action, data)).value;
  const sessionId = before.sessionId;
  await rpc('session.create', { sessionId, cwd: before.workspaceRoot });
  const config = await api('config', { sessionId }); assert.equal(config.version, 1); assert.equal(config.enabled, true);
  const effects = await api('effect-graph', { sessionId }); assert.deepEqual(effects, before.effects);
  assert.equal(await readFile(join(before.workspaceRoot, 'acceptance.txt'), 'utf8'), before.artifact);
  const memories = await api('memories', { sessionId, filters: { kind: 'semantic' } });
  assert.equal(memories[0].version, 1); assert.equal(Object.hasOwn(memories[0], 'content'), false);
  const memory = await api('memory', { sessionId, memoryId: memories[0].id, version: memories[0].version });
  assert.equal(memory.content.nodes[0].exactContent, before.artifact);
  const history = await rpc('session.history', { sessionId, maxMessages: 200 });
  assert.equal(history.events.filter(e => e.event.type === 'turn/end').length, 1);
  assert.equal((await rpc('session.list', {})).items.find(s => s.sessionId === sessionId).running, false);
  assert.deepEqual(await (await fetch(url + '/vault/status')).json(), { initialized: true, locked: true });
  browser = await chromium.launchPersistentContext(join(root, 'reopen-browser-profile'), { headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', viewport: { width: 1440, height: 1000 } });
  const page = await browser.newPage(); await page.goto(url + '/memory-recovery');
  await page.locator('#sessions').selectOption(sessionId); await page.locator('#enabled').waitFor();
  await page.locator('#effects').click(); await page.waitForFunction(() => document.getElementById('graph-output').textContent.includes('COMMITTED'));
  await page.screenshot({ path: join(root, 'reopened-effects.png'), fullPage: true });
  await page.locator('#open-chat').click();
  const continueButton = page.getByRole('button', { name: /^(继续|Continue)$/ });
  const completedText = page.getByText('PAGED_SANDBOX_ACCEPTANCE_OK', { exact: false }).first();
  // A retained profile may already have accepted the notice. Wait for an observed
  // gate OR the real completed chat, never swallow a timeout of the final assertion.
  await Promise.race([continueButton.waitFor({ state: 'visible' }), completedText.waitFor({ state: 'visible' })]);
  if (await continueButton.isVisible()) await continueButton.click();
  await completedText.waitFor({ state: 'visible' });
  await page.screenshot({ path: join(root, 'reopened-native-model-chat.png'), fullPage: true });
  const report = { reopened: true, url, sessionId, configVersion: config.version, effectId: effects.nodes[0].id, effectState: effects.nodes[0].state, artifactUnchanged: true, semanticVersion: memories[0].version, vaultLocked: true, newModelRequests: 0 };
  await writeFile(join(root, 'reopen-report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(report));
} catch (error) {
  await writeFile(join(root, `reopen-failure-${Date.now()}.json`), JSON.stringify({ reopened: false, error: error.message }, null, 2), { mode: 0o600 });
  throw error;
} finally {
  if (browser) await browser.close(); child.kill('SIGTERM'); await exit;
  await writeFile(join(root, `reopen-native-web-${Date.now()}.log`), log, { mode: 0o600 });
}

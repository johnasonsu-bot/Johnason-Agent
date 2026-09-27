import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { authenticateWeb, redactLaunchOutput } from './fixtures/native-web-client.mjs';

async function loopbackModel(t) {
  const requests = [];
  const server = createServer(async (request, response) => {
    try {
      const parts = [];
      for await (const part of request) parts.push(part);
      requests.push({ path: request.url, body: JSON.parse(Buffer.concat(parts).toString()) });
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.end([
        { choices: [{ delta: { role: 'assistant', content: null, reasoning_content: '' } }] },
        { choices: [{ delta: { content: 'LOCAL_EXTENSION_OK' } }] },
        { choices: [{ delta: { content: '' }, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 1 } },
        '[DONE]',
      ].map(event => `data: ${typeof event === 'string' ? event : JSON.stringify(event)}\n\n`).join(''));
    } catch (error) { response.destroy(error); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  return { requests, baseURL: `http://127.0.0.1:${server.address().port}` };
}

async function launchProfile(t, baseURL, { missingVersion = false, protocol, useVault = false } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'dsh-request-extension-'));
  const entries = [{ id: 'request-extension-acceptance',
    name: fileURLToPath(new URL('./fixtures/request-extension-plugin.mjs', import.meta.url)),
    config: { baseURL, protocol, useVault } }];
  if (missingVersion) {
    const bad = join(root, 'missing-version');
    await mkdir(bad);
    await writeFile(join(bad, 'package.json'), JSON.stringify({ name: '@johnason/regression-missing-version', type: 'module' }));
    await writeFile(join(bad, 'index.mjs'), 'export function apply() {}\n');
    entries.push({ id: 'missing-version', name: join(bad, 'index.mjs') });
  }
  const patch = join(root, 'request-extension.patch.json');
  await writeFile(patch, JSON.stringify([{ insert: entries }]));
  const probe = createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
  const child = spawn(process.execPath, [cli, 'web', '--data-dir', root, '--port', String(port), '--no-open', '--patch', patch], {
    env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', data => { output += data; });
  child.stderr.on('data', data => { output += data; });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
    await exited;
  });
  const deadline = Date.now() + 30000;
  while (!output.includes(`dsh web: http://127.0.0.1:${port}`)) {
    assert.equal(child.exitCode, null, redactLaunchOutput(output));
    assert.ok(Date.now() < deadline, `Native profile startup timed out: ${redactLaunchOutput(output)}`);
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  const client = await authenticateWeb(output, `http://127.0.0.1:${port}`);
  t.diagnostic(`Retained isolated request-extension profile: ${root}`);
  return { client, root };
}

// A missing version on either actual local package must fail this test before HTTP dispatch.
test('native local plugins pass official inventory preparation and a loopback DeepSeek streaming request', { timeout: 45000 }, async t => {
  const model = await loopbackModel(t);
  const { client, root } = await launchProfile(t, model.baseURL);
  const response = await client.fetch('/request-extension-acceptance');
  const result = await response.json();
  assert.equal(response.status, 200, JSON.stringify(result));
  assert.deepEqual(result.finish, { kind: 'stop' });
  assert.equal(result.blocks.filter(block => block.type === 'text').map(block => block.text).join(''), 'LOCAL_EXTENSION_OK');
  assert.equal(model.requests.length, 1);
  assert.equal(model.requests[0].path, '/chat/completions');
  const inventory = model.requests[0].body.dsh_plugin_packages;
  assert.deepEqual(inventory, result.fields.dsh_plugin_packages);
  assert.equal(inventory.version, 1);
  for (const manifestUrl of [new URL('../package.json', import.meta.url), new URL('../browser/package.json', import.meta.url), new URL('../model-api-browser/package.json', import.meta.url), new URL('../local-model-browser/package.json', import.meta.url)]) {
    const manifest = JSON.parse(await readFile(manifestUrl, 'utf8'));
    assert.ok(inventory.packages.some(pkg => pkg.name === manifest.name && pkg.version === manifest.version), `${manifest.name} must reach the official request with its version`);
    assert.match(manifest.version, /^\d+\.\d+\.\d+(?:-[\w.-]+)?$/);
  }
  const copied = JSON.parse(await readFile(join(root, 'profiles/web/node_modules/@johnason/dsh-memory-ui/package.json'), 'utf8'));
  assert.ok(inventory.packages.some(pkg => pkg.name === copied.name && pkg.version === copied.version));
});

test('native official inventory keeps rejecting missing package versions before sending a model request', { timeout: 45000 }, async t => {
  const model = await loopbackModel(t);
  const { client } = await launchProfile(t, model.baseURL, { missingVersion: true });
  const response = await client.fetch('/request-extension-acceptance');
  const result = await response.json();
  assert.equal(response.status, 503);
  assert.equal(result.code, 'REQUEST_EXTENSION');
  assert.match(result.cause, /missing-version\/package\.json must declare non-empty name and version/);
  assert.equal(model.requests.length, 0, 'Metadata failure must prevent HTTP dispatch');
});

test('official Messages adapter reports the locked Vault action instead of a transport failure', { timeout: 45000 }, async t => {
  const model = await loopbackModel(t);
  const { client } = await launchProfile(t, model.baseURL, { protocol: 'messages', useVault: true });
  const password = randomUUID();
  const initialize = await client.fetch('/vault/initialize', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password, confirmation: password }) });
  assert.equal(initialize.status, 200, await initialize.text());
  const lock = await client.fetch('/vault/lock', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: '{}' });
  assert.equal(lock.status, 200, await lock.text());
  assert.deepEqual(await (await client.fetch('/vault/status')).json(), { initialized: true, locked: true });
  const response = await client.fetch('/request-extension-acceptance');
  const result = await response.json();
  assert.equal(response.status, 503);
  assert.equal(result.code, 'VAULT_LOCKED', JSON.stringify(result));
  assert.match(result.error, /Vault/);
  assert.match(result.error, /解锁|unlock/i);
  assert.match(result.error, /\/vault/);
  assert.doesNotMatch(result.error, /transport failed/i);
  assert.equal(model.requests.length, 0, 'Locked model credentials must fail before HTTP dispatch');
  assert.deepEqual(await (await client.fetch('/vault/status')).json(), { initialized: true, locked: true });
});

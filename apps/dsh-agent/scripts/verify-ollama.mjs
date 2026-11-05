#!/usr/bin/env node
// Opt-in live smoke: a new native DSH profile, an ephemeral test Vault password,
// a real local Ollama model and a persisted agent turn. No user data is opened.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { ecosystemOverlay } from '../src/ecosystem.mjs';
import { authenticateWeb, redactLaunchOutput } from '../tests/fixtures/native-web-client.mjs';

const root = await mkdtemp(join(tmpdir(), 'johnason-ollama-live-'));
const probe = createServer();
await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
const port = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const origin = `http://127.0.0.1:${port}`;
const overlay = ecosystemOverlay();
overlay.find(row => row.id === 'llm-pi-ai').config.providers['johnason-ollama'].models[0].maxTokens = 128;
const patch = join(root, 'ollama.patch.json');
await writeFile(patch, JSON.stringify(overlay), { mode: 0o600 });
const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
const child = spawn(process.execPath, [cli, 'web', '--data-dir', root, '--local-ollama', '--port', String(port), '--patch', patch, '--no-open'], {
  env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
});
let output = '';
child.stdout.on('data', data => { output += data; });
child.stderr.on('data', data => { output += data; });
const exited = new Promise(resolve => child.once('exit', resolve));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
  const launchBy = Date.now() + 30000;
  while (!output.includes(`dsh web: ${origin}`)) {
    assert.equal(child.exitCode, null, redactLaunchOutput(output));
    assert.ok(Date.now() < launchBy, redactLaunchOutput(output));
    await pause(100);
  }
  const client = await authenticateWeb(output, origin);
  let password = randomUUID();
  const initialized = await client.fetch('/vault/initialize', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password, confirmation: password }) });
  assert.equal(initialized.status, 200);
  password = '';
  const created = await client.rpc('workspace/create', { request: { path: root } });
  const { sessionId } = await client.rpc('session/create', { request: { workspaceId: created.workspace.workspaceId } });
  await client.rpc('session/selectModel', { request: { sessionId, provider: 'johnason-ollama', model: 'gemma4:e4b' } });
  await client.rpc('session/prompt', { request: { requestId: randomUUID(), sessionId, mode: 'queue', content: [{ type: 'text', text: 'Reply with exactly OLLAMA_DSH_OK. Do not call any tools.' }] } });
  const finishBy = Date.now() + 180000;
  let text = '';
  while (true) {
    const history = await client.history(sessionId);
    const ended = history.records.find(row => row.event?.type === 'turn/end');
    if (ended?.event.data.reason?.kind === 'error') throw new Error(`Native Ollama turn failed: ${JSON.stringify(ended.event.data.reason)}`);
    text = history.records.filter(row => row.event?.type === 'assistant/message').flatMap(row => row.event.data.message.content).filter(block => block.type === 'text').map(block => block.text).join('\n');
    if (ended && text.includes('OLLAMA_DSH_OK')) break;
    assert.ok(Date.now() < finishBy, 'Native Ollama turn timed out');
    await pause(300);
  }
  const receipt = { status: 'passed', provider: 'johnason-ollama', model: 'gemma4:e4b', response: text, sessionId, isolatedDataRoot: root, verifiedAt: new Date().toISOString() };
  await writeFile(join(root, 'verification.json'), JSON.stringify(receipt, null, 2) + '\n', { mode: 0o600 });
  process.stdout.write(JSON.stringify(receipt) + '\n');
} finally {
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
  await exited;
}

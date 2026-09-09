// Explicit live acceptance only: one NEW native model task, never an existing session.
// Retains all fixtures. Run --serve-only for a fresh manual environment without generation.
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { validateMemoryAcceptance } from './memory-acceptance-result.mjs';

const root = await mkdtemp(join(homedir(), 'dsh-memory-live-'));
const workspaceRoot = join(root, 'workspace'); await mkdir(workspaceRoot);
const probe = createServer(); await new Promise(r => probe.listen(0, '127.0.0.1', r));
const port = probe.address().port; await new Promise(r => probe.close(r));
const url = `http://127.0.0.1:${port}`;
const model = 'qwen3.8-27b-uncensored-mlx';
const overlay = join(root, 'local-model.patch.json');
await writeFile(overlay, JSON.stringify([
  { id: 'llm-pi-ai', config: { providers: { 'local-acceptance': { api: 'openai-completions', baseURL: 'http://127.0.0.1:1234/v1',
    // This is not a credential. The OpenAI SDK requires a nonempty auth header even
    // for a local server that ignores authentication; never copy a user's API key.
    headers: { Authorization: 'Anonymous local-only' },
    models: [{ id: model, name: 'Local Qwen acceptance', contextWindow: 208384, maxTokens: 4096, reasoningEfforts: false }],
    compat: { maxTokensField: 'max_tokens' }, streamIdleTimeoutMs: 300000,
    retryPolicy: { mode: 'normal', maxRetries: 0, retryableCodes: ['RATE_LIMIT', 'SERVER', 'TRANSPORT'] },
  } } } },
  { id: 'agent-default-model', config: { provider: 'local-acceptance', model } },
  { id: 'session-title-llm', disabled: true },
], null, 2), { mode: 0o600 });
const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
const args = [cli, 'web', '--data-dir', root, '--workspace', workspaceRoot, '--port', String(port), '--patch', overlay, '--no-open'];
const child = spawn(process.execPath, args, { env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'] });
let log = ''; for (const stream of [child.stdout, child.stderr]) stream.on('data', b => { log += b; process.stdout.write(b); });
const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
process.on('SIGTERM', () => child.kill('SIGTERM')); process.on('SIGINT', () => child.kill('SIGTERM'));
console.log(JSON.stringify({ root, workspaceRoot, url, overlay, model, start: [process.execPath, ...args] }));
const deadline = Date.now() + 30000;
while (!log.includes(`dsh web: ${url}`)) {
  if (child.exitCode !== null || Date.now() > deadline) {
    if (child.exitCode === null) child.kill('SIGTERM'); await exited;
    await writeFile(join(root, 'native-web.log'), log, { mode: 0o600 });
    throw Error('Native Web startup failed before any session or model request');
  }
  await new Promise(r => setTimeout(r, 100));
}
if (process.argv.includes('--serve-only')) { await exited; process.exit(); }
async function post(path, body) { const response = await fetch(url + path, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' }, body: JSON.stringify(body) }); const data = await response.json(); if (!response.ok) throw Error(JSON.stringify(data)); return data; }
async function api(action, body = {}) { return (await post('/memory-recovery/api/' + action, body)).value; }
async function rpc(method, payload) { const response = await post('/api/' + method, { type: 'client-request', rpcId: randomUUID(), method, payload }); if (!response.result.ok) throw Error(JSON.stringify(response.result)); return response.result.value; }
let report = { root, workspaceRoot, url, model, startedAt: Date.now(), success: false };
try {
  // Isolated new empty Vault only; password is transient, never logged or written.
  let password = randomUUID() + randomUUID(); await post('/vault/initialize', { password, confirmation: password }); password = '';
  const { sessionId } = await api('create-session', { workspaceRoot }); report.sessionId = sessionId;
  await api('configure', { sessionId, expectedVersion: 0, input: { enabled: true, projectId: 'live-acceptance', agentId: sessionId, workspaceRoot,
    sandbox: { sandbox_required: true, mode: 'workspace-write', network: 'host', workspaceRoot, timeoutMs: 15000 }, budgetTokens: 6000, anchors: ['Only perform the explicitly requested acceptance task. No retries and no unrelated file access.'] } });
  const record = await api('put-memory', { sessionId, reason: 'Explicit live acceptance fixture', record: { kind: 'semantic', visibility: 'private', summary: 'Acceptance contract: read its full page for the required file content.', content: { nodes: [{ id: 'acceptance-contract', type: 'contract', file: 'acceptance.txt', exactContent: 'PAGED_SANDBOX_ACCEPTANCE_OK\n' }], edges: [] } } });
  const prompt = `Perform this bounded acceptance task, then stop. Call memory_page_in with id ${JSON.stringify(record.id)}, version 1, maxChars 2000. After that tool completes, read the next logged memory context, and call memory_sandbox_run exactly once with argv [${JSON.stringify(process.execPath)},"-e", a short JavaScript string using node:fs.writeFileSync] to write the contract's exactContent into its file, relative to the current workspace. Do not use any other tools, no searching, no retries. Finally report the file name and the actual tool result. Maximum 3 tool calls. Do not merely describe the calls: invoke them.`;
  await rpc('session.prompt', { sessionId, mode: 'queue', content: [{ type: 'text', text: prompt }] });
  const until = Date.now() + 720000;
  let history;
  while (true) {
    history = await rpc('session.history', { sessionId, maxMessages: 200 });
    const events = history.events.map(e => e.event);
    const end = events.findLast(e => e.type === 'turn/end');
    if (end) { report.turnEnd = end; break; }
    if (Date.now() > until || events.filter(e => e.type === 'step/start').length > 6) { await rpc('session.cancel', { sessionId }); report.limit = 'Bounded acceptance stopped at 12 minutes or >6 steps; no retry'; break; }
    console.log(JSON.stringify({ phase: 'native-model-running', seconds: Math.round((Date.now() - report.startedAt) / 1000), events: events.length, last: events.at(-1)?.type }));
    await new Promise(r => setTimeout(r, 15000));
  }
  await api('barrier', { sessionId });
  report.history = history;
  report.effects = await api('effect-graph', { sessionId });
  report.status = await api('status', { sessionId });
  report.memories = await api('memories', { sessionId, filters: { limit: 1000 } });
  try { report.artifact = await readFile(join(workspaceRoot, 'acceptance.txt'), 'utf8'); } catch { report.artifact = null; }
  const events = history.events.map(e => e.event);
  const calls = events.filter(e => e.type === 'assistant/message').flatMap(e => e.data.message.content.filter(b => b.type === 'tool-call').map(b => b.name));
  report.calls = calls;
  report.success = validateMemoryAcceptance(report);
} catch (error) { report.error = error.message; }
finally {
  report.finishedAt = Date.now();
  await writeFile(join(root, 'acceptance-report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
  child.kill('SIGTERM'); await exited;
  await writeFile(join(root, 'native-web.log'), log, { mode: 0o600 });
}
console.log(JSON.stringify({ root, success: report.success, calls: report.calls, artifact: report.artifact, error: report.error }));
process.exitCode = report.success ? 0 : 1;

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp, mkdir, copyFile, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';
import { authenticateWeb, redactLaunchOutput } from './fixtures/native-web-client.mjs';

const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
const fixture = name => fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url));

test('CLI config dump uses built composition without starting Vault or model services', { timeout: 30000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-cli-dump-'));
  const output = execFileSync(process.execPath, [cli, 'headless', '--data-dir', root, '--workspace', root, '--dump-config'], {
    env: { PATH: process.env.PATH, HOME: root }, encoding: 'utf8', timeout: 25000,
  });
  assert.match(output, /dsh-encrypted-base/);
  assert.match(output, /local-models/);
  assert.equal(existsSync(join(root, 'vault.enc')), false);
  assert.equal(existsSync(join(root, 'memory/recovery.sqlite')), false);
  t.diagnostic(`retained config-dump fixture: ${root}`);
});
async function portProbe() {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { server, port: server.address().port };
}
function launch(t, root, port, extra = []) {
  const child = spawn(process.execPath, [cli, 'web', '--data-dir', root, '--port', String(port), '--no-open', ...extra], {
    env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', bytes => { output += bytes; });
  child.stderr.on('data', bytes => { output += bytes; });
  const exit = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM'); await exit; });
  return { child, exit, output: () => redactLaunchOutput(output), authenticate: () => authenticateWeb(output, `http://127.0.0.1:${port}`) };
}
async function waitFor(check, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('Timed out awaiting native process condition');
}
async function ready(process, port) {
  await waitFor(() => {
    assert.equal(process.child.exitCode, null, process.output());
    return process.output().includes(`dsh web: http://127.0.0.1:${port}`);
  });
}
// Catches accidental port auto-increment, swallowed bind errors, or killing a foreign listener.
test('occupied port fails visibly and leaves the existing listener alive', { timeout: 40000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-port-conflict-'));
  const { server, port } = await portProbe();
  t.after(() => new Promise(resolve => server.close(resolve)));
  const process = launch(t, root, port);
  const result = await process.exit;
  assert.notEqual(result.code, 0);
  assert.match(process.output(), /EADDRINUSE|address already in use/i);
  assert.equal(server.listening, true);
});

// Catches lost DSH_HOME/profile overlays, native plugin resolution and restart-persistence regressions.
test('native profile loads local MCP and Skill; normal restart restores workspace/session and relocks Vault', { timeout: 80000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-restart-'));
  const workspace = await mkdtemp(join(tmpdir(), 'dsh-skill-workspace-'));
  // TMPDIR may be inside the candidate repository. Give this fixture its own
  // project root so native skill discovery cannot ascend into the parent repo.
  execFileSync('git', ['init', '--quiet', workspace]);
  await mkdir(join(workspace, '.dsh/skills/dsh-acceptance-marker'), { recursive: true });
  await copyFile(fixture('acceptance-skill/SKILL.md'), join(workspace, '.dsh/skills/dsh-acceptance-marker/SKILL.md'));
  const patch = join(root, 'fixture.patch.json');
  await writeFile(patch, JSON.stringify([{ insert: [
    { id: 'acceptance-mcp', name: fileURLToPath(new URL('../../../third_party/deepseek-harness/packages/mcp/mcp-client/src/index.ts', import.meta.url)), config: { transport: 'stdio', serverName: 'acceptance', command: process.execPath, args: [fixture('mcp-server.mjs')], env: {}, cwd: workspace, failOnStartupError: true, toolCallTimeoutMs: 5000 } },
    { id: 'acceptance-profile', name: fixture('profile-plugin.mjs') },
  ] }]));
  const { server, port } = await portProbe();
  await new Promise(resolve => server.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const first = launch(t, root, port, ['--patch', patch]);
  await ready(first, port);
  const firstClient = await first.authenticate();
  await waitFor(async () => (await firstClient.fetch('/acceptance-fixture')).status === 200);
  const marker = await (await firstClient.fetch('/acceptance-fixture')).json();
  assert.equal(marker.source, 'dsh-acceptance-profile-plugin');
  assert.equal(marker.result.isError, false);
  assert.deepEqual(marker.result.content, [{ type: 'text', text: 'DSH_LOCAL_MCP_OK' }]);
  const created = await firstClient.rpc('workspace/create', { request: { path: workspace } });
  const { sessionId } = await firstClient.rpc('session/create', { request: { workspaceId: created.workspace.workspaceId } });
  await firstClient.rpc('session/rename', { request: { sessionId, title: 'Local restart acceptance' } });
  const skills = await firstClient.rpc('skills/list', { request: { sessionId } });
  assert.ok(skills.skills.some(skill => skill.name === 'dsh-acceptance-marker'), JSON.stringify(skills));
  const password = 'synthetic-acceptance-password';
  const initialized = await firstClient.fetch('/vault/initialize', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password, confirmation: password }) });
  assert.equal(initialized.status, 200, await initialized.text());
  const encrypted = await readFile(join(root, 'vault.enc'));
  first.child.kill('SIGTERM');
  assert.deepEqual(await first.exit, { code: 0, signal: null });
  const second = launch(t, root, port, ['--patch', patch]);
  await ready(second, port);
  const secondClient = await second.authenticate();
  assert.deepEqual(await (await secondClient.fetch('/vault/status')).json(), { initialized: true, locked: true });
  assert.deepEqual(await readFile(join(root, 'vault.enc')), encrypted);
  const workspaceFrame = await secondClient.firstFrame('workspace/follow', {});
  assert.equal(workspaceFrame.type, 'baseline');
  const workspaces = workspaceFrame.value;
  assert.ok(workspaces.items.some(item => item.workspaceId === created.workspace.workspaceId && item.sessionIds.includes(sessionId)));
  const sessions = await secondClient.rpc('session/list', { _request: {} });
  assert.ok(sessions.items.some(item => item.sessionId === sessionId && item.running === false));
  const history = await secondClient.history(sessionId);
  assert.match(JSON.stringify(history), /Local restart acceptance/);
  assert.equal(first.output().includes(password), false);
  assert.equal(second.output().includes(password), false);
  second.child.kill('SIGTERM');
  assert.deepEqual(await second.exit, { code: 0, signal: null });
  await assert.rejects(fetch(url));
  t.diagnostic(`retained test roots: ${root}, ${workspace}; session: ${sessionId}`);
});

test('CLI plugin list operates on a new isolated native profile without credentials', { timeout: 40000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'dsh-cli-plugin-'));
  const child = spawn(process.execPath, [cli, 'plugin', '--data-dir', root, '--workspace', root, '--profile', 'acceptance-cli', 'list', '--depth', '0'], {
    env: { PATH: process.env.PATH, HOME: root }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', bytes => { output += bytes; });
  child.stderr.on('data', bytes => { output += bytes; });
  const exit = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM'); await exit; });
  assert.deepEqual(await exit, { code: 0, signal: null }, output);
  const manifest = JSON.parse(await readFile(join(root, 'profiles/acceptance-cli/package.json'), 'utf8'));
  assert.ok(Array.isArray(manifest.dsh.profile.bundles));
});

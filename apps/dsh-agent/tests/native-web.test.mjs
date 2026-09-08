import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp, writeFile, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

test('real isolated native Web serves native UI plus Vault and exits after SIGTERM', { timeout: 40000 }, async t => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'dsh-real-web-'));
  await writeFile(join(dataRoot, '.env'), 'DEEPSEEK_API_KEY=synthetic-secret-do-not-use\nDSH_HOME=/invalid/old-home\n');
  const probe = createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));
  const child = spawn(process.execPath, [cli, 'web', '--data-dir', dataRoot, '--port', String(port), '--no-open'], {
    env: { PATH: process.env.PATH, HOME: dataRoot, DEEPSEEK_API_KEY: 'synthetic-secret-do-not-use' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', data => { output += data; });
  child.stderr.on('data', data => { output += data; });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  t.after(async () => { if (child.exitCode === null) child.kill('SIGTERM'); await exited; });
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Native Web did not start: ${output}`)), 30000);
    const interval = setInterval(() => {
      if (output.includes(`dsh web: http://127.0.0.1:${port}`)) { clearInterval(interval); clearTimeout(timeout); resolve(); }
      else if (child.exitCode !== null) { clearInterval(interval); clearTimeout(timeout); reject(new Error(output)); }
    }, 50);
    t.after(() => { clearTimeout(timeout); clearInterval(interval); });
  });
  const url = `http://127.0.0.1:${port}`;
  const index = await (await fetch(url)).text();
  assert.match(index, /__DSH_BOOT__|__ModuleLoader__/);
  assert.match(index, /href="\/vault"/);
  assert.equal((await fetch(`${url}/vault`)).status, 200);
  assert.deepEqual(await (await fetch(`${url}/vault/status`)).json(), { initialized: false, locked: true });
  await assert.rejects(access(join(dataRoot, '.credentials.yaml')), { code: 'ENOENT' });
  assert.equal(output.includes('synthetic-secret-do-not-use'), false);
  child.kill('SIGTERM');
  assert.deepEqual(await exited, { code: 0, signal: null });
  await assert.rejects(fetch(url));
});

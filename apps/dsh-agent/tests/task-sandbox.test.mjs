import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, existsSync, symlinkSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { validateSandboxRequirements, runSandboxTask } from '../src/task-sandbox.mjs';

const requireNative = createRequire(new URL('../../../third_party/deepseek-harness/packages/sandbox/sandbox-local/package.json', import.meta.url));
const { Context } = requireNative('@deepseek-ai/cordis');
const { LocalSandboxProvider } = await import(new URL('../../../third_party/deepseek-harness/packages/sandbox/sandbox-local/lib/index.js', import.meta.url));
async function fixture(t, config = {}) {
  // Outside system temp: Seatbelt deliberately grants /tmp and Darwin temp.
  const root = mkdtempSync(join(homedir(), 'dsh-task-sandbox-'));
  const workspaceRoot = join(root, 'workspace');
  const protectedRoot = join(root, 'protected');
  mkdirSync(workspaceRoot); mkdirSync(protectedRoot);
  t.diagnostic(`Retained fixture: ${root}`);
  const ctx = new Context();
  await ctx.plugin(LocalSandboxProvider, config);
  t.after(() => ctx.fiber.dispose());
  return { root, workspaceRoot, protectedRoot, sandbox: ctx.sandbox,
    requirements: { sandbox_required: true, mode: 'workspace-write', network: 'host', workspaceRoot } };
}

test('actual native confinement allows workspace write, rejects sibling and symlink escape', async t => {
  const f = await fixture(t);
  const run = target => runSandboxTask({ ...f, argv: [process.execPath, '-e', 'require("node:fs").writeFileSync(process.argv[1],"confined")', target] });
  const allowed = await run(join(f.workspaceRoot, 'allowed'));
  assert.equal(allowed.exitCode, 0);
  assert.equal(allowed.enforcement, 'full');
  assert.equal(readFileSync(join(f.workspaceRoot, 'allowed'), 'utf8'), 'confined');
  const denied = await run(join(f.protectedRoot, 'denied'));
  assert.notEqual(denied.exitCode, 0);
  assert.equal(denied.denied, true);
  assert.equal(existsSync(join(f.protectedRoot, 'denied')), false);
  symlinkSync(f.protectedRoot, join(f.workspaceRoot, 'escape'));
  assert.notEqual((await run(join(f.workspaceRoot, 'escape', 'denied'))).exitCode, 0);
  assert.equal(existsSync(join(f.protectedRoot, 'denied')), false);
  t.diagnostic(`Native backend: ${allowed.backend}; enforcement: ${allowed.enforcement}; denial: ${denied.stderr.slice(0, 250)}`);
});

test('requirements fail closed for unsupported isolation and nonexistent directory', async t => {
  const f = await fixture(t);
  for (const extra of [{ network: 'none' }, { cpu: 1 }, { memory: 1024 }, { container: true }, { mode: 'danger-full-access' }, { network: 'host', mystery: true }]) {
    assert.throws(() => validateSandboxRequirements({ ...f.requirements, ...extra }), { code: 'SANDBOX_REQUIREMENT_UNSUPPORTED' });
  }
  assert.throws(() => validateSandboxRequirements({ ...f.requirements, workspaceRoot: join(f.root, 'missing') }), { code: 'SANDBOX_INVALID_WORKSPACE' });
  await assert.rejects(runSandboxTask({ ...f, sandbox: null, argv: ['true'] }), { code: 'SANDBOX_UNAVAILABLE' });
});

test('missing actual runner is an availability failure without unconfined fallback', async t => {
  const f = await fixture(t, { runnerCommand: ['/no-such-dsh-runner'], runnerFailureSignatures: ['runner failed'] });
  await assert.rejects(runSandboxTask({ ...f, argv: [process.execPath, '-e', 'require("node:fs").writeFileSync("must-not-exist","bad")'] }), { code: 'SANDBOX_UNAVAILABLE' });
  assert.equal(existsSync(join(f.workspaceRoot, 'must-not-exist')), false);
});

test('timeout and cancellation await termination and preserve unknown side effects', async t => {
  const f = await fixture(t);
  const argv = [process.execPath, '-e', 'require("node:fs").writeFileSync("started","1");setInterval(()=>{},1000)'];
  const timed = await runSandboxTask({ ...f, argv, requirements: { ...f.requirements, timeoutMs: 300 } });
  assert.equal(timed.state, 'UNKNOWN');
  assert.equal(timed.timedOut, true);
  assert.equal(readFileSync(join(f.workspaceRoot, 'started'), 'utf8'), '1');
  const controller = new AbortController();
  const pending = runSandboxTask({ ...f, argv, signal: controller.signal });
  setTimeout(() => controller.abort(), 100);
  const cancelled = await pending;
  assert.equal(cancelled.cancelled, true);
  assert.equal(cancelled.state, 'UNKNOWN');
  const before = new AbortController(); before.abort();
  const notStarted = await runSandboxTask({ ...f, argv, signal: before.signal });
  assert.equal(notStarted.state, 'ABORTED');
  assert.equal(notStarted.started, false);
});

test('bounded output and argv stay literal with read-only enforcement', async t => {
  const f = await fixture(t);
  const result = await runSandboxTask({ ...f, argv: [process.execPath, '-e', 'console.log(process.argv[1]);console.log("x".repeat(10000))', '$(touch surprise)'], requirements: { ...f.requirements, maxOutputBytes: 128 } });
  assert.ok(result.stdout.startsWith('$(touch surprise)'));
  assert.ok(Buffer.byteLength(result.stdout) <= 128);
  assert.equal(result.outputTruncated, true);
  assert.equal(existsSync(join(f.workspaceRoot, 'surprise')), false);
  const denied = await runSandboxTask({ ...f, argv: [process.execPath, '-e', 'require("node:fs").writeFileSync("readonly-denied","bad")'], requirements: { ...f.requirements, mode: 'read-only' } });
  assert.equal(denied.denied, true);
  assert.equal(existsSync(join(f.workspaceRoot, 'readonly-denied')), false);
});

test('filesystem canonicalization preserves symlink-dotdot identity and UTF-8 byte bounds', async t => {
  const f = await fixture(t);
  mkdirSync(join(f.protectedRoot, 'inner'));
  symlinkSync(join(f.protectedRoot, 'inner'), join(f.workspaceRoot, 'link'));
  const actual = validateSandboxRequirements({ ...f.requirements, workspaceRoot: `${f.workspaceRoot}/link/..` });
  assert.equal(actual.workspaceRoot, realpathSync(f.protectedRoot));
  const result = await runSandboxTask({ ...f, argv: [process.execPath, '-e', 'process.stdout.write("中中中")'], requirements: { ...f.requirements, maxOutputBytes: 4 } });
  assert.ok(Buffer.byteLength(result.stdout) <= 4);
  assert.equal(result.stdout, '中');
});

test('cancel kills same-group descendants even after parent closes its output', async t => {
  const f = await fixture(t);
  const controller = new AbortController();
  const descendantScript = 'process.on("SIGTERM",()=>{});setInterval(()=>{},1000)';
  const script = `const {spawn}=require("node:child_process");const child=spawn(process.execPath,["-e",${JSON.stringify(descendantScript)}],{stdio:"ignore"});require("node:fs").writeFileSync("descendant",String(child.pid));setInterval(()=>{},1000)`;
  const pending = runSandboxTask({ ...f, argv: [process.execPath, '-e', script], signal: controller.signal });
  setTimeout(() => controller.abort(), 150);
  const outcome = await pending;
  assert.equal(outcome.cancelled, true, outcome.stderr);
  const pid = Number(readFileSync(join(f.workspaceRoot, 'descendant'), 'utf8'));
  await new Promise(resolve => setTimeout(resolve, 250));
  let alive = true;
  try { process.kill(pid, 0); } catch (error) { if (error.code === 'ESRCH') alive = false; else throw error; }
  // Ensure a failed regression test does not retain an intentionally stuck process.
  if (alive) process.kill(pid, 'SIGKILL');
  assert.equal(alive, false);
});

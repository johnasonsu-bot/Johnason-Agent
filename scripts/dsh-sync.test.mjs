import assert from 'node:assert/strict';
import { test } from 'node:test';
import { syncUpstream, parseArgs, runCli } from './dsh-sync.mjs';

const OLD = '1111111111111111111111111111111111111111';
const NEW = '2222222222222222222222222222222222222222';
const LATER = '3333333333333333333333333333333333333333';

// GitHub is the only mocked boundary: production code chooses and validates
// commands, metadata, versions and success. No live token or write is needed.
function github({ forkSha = OLD, upstreamSha = NEW, comparison = { status: 'ahead', ahead_by: 12701, behind_by: 0 }, login = 'johnasonsu-bot', parent = 'deepseek-ai/deepseek-harness', branch = 'master', failAt, postFork, postUpstream, version = '0.2.2-rc.1' } = {}) {
  const calls = [];
  let synced = false;
  const run = async (command, args) => {
    calls.push([command, ...args]);
    assert.equal(command, 'gh');
    assert.equal(args.includes('--force'), false);
    if (args[0] === 'repo') {
      assert.deepEqual(args, ['repo', 'sync', 'johnasonsu-bot/deepseek-harness', '--source', 'deepseek-ai/deepseek-harness', '--branch', 'master']);
      if (failAt === 'sync') return { status: 1, stdout: '', stderr: 'secret-canary-do-not-print' };
      synced = true;
      return { status: 0, stdout: '', stderr: '' };
    }
    assert.deepEqual(args.slice(0, 5), ['api', '--hostname', 'github.com', '--method', 'GET']);
    const path = args[5];
    if (failAt && path.includes(failAt)) return { status: 1, stdout: '', stderr: 'secret-canary-do-not-print' };
    let data;
    if (path === 'user') data = { login };
    else if (path === 'repos/deepseek-ai/deepseek-harness') data = { full_name: 'deepseek-ai/deepseek-harness', owner: { login: 'deepseek-ai' }, fork: false, default_branch: 'master' };
    else if (path === 'repos/johnasonsu-bot/deepseek-harness') data = { full_name: 'johnasonsu-bot/deepseek-harness', owner: { login: 'johnasonsu-bot' }, fork: true, parent: { full_name: parent }, default_branch: branch };
    else if (path === 'repos/deepseek-ai/deepseek-harness/git/ref/heads/master') data = { ref: 'refs/heads/master', object: { type: 'commit', sha: synced ? postUpstream ?? upstreamSha : upstreamSha } };
    else if (path === 'repos/johnasonsu-bot/deepseek-harness/git/ref/heads/master') data = { ref: 'refs/heads/master', object: { type: 'commit', sha: synced ? postFork ?? upstreamSha : forkSha } };
    else if (path.startsWith('repos/deepseek-ai/deepseek-harness/contents/apps/cli/package.json?ref=')) data = { encoding: 'base64', content: Buffer.from(JSON.stringify({ name: '@deepseek-ai/dsh', version })).toString('base64') };
    else if (path === `repos/johnasonsu-bot/deepseek-harness/compare/${forkSha}...deepseek-ai:${upstreamSha}?per_page=1&page=1`) data = comparison;
    else assert.fail(`Unexpected GitHub route: ${path}`);
    return { status: 0, stdout: JSON.stringify(data), stderr: '' };
  };
  return { run, calls, mutated: () => synced };
}

test('default mode reports 12,701 upstream commits without relying on the paginated commit list', async () => {
  const api = github();
  const result = await syncUpstream({ run: api.run });
  assert.equal(result.status, 'update_available');
  assert.equal(result.upstream.sha, NEW);
  assert.equal(result.upstream.version, '0.2.2-rc.1');
  assert.equal(result.fork.sha, OLD);
  assert.equal(result.aheadBy, 12701);
  assert.equal(result.behindBy, 0);
  assert.equal(api.mutated(), false);
});

test('apply safely syncs the expected fork and confirms its live SHA', async () => {
  const api = github();
  const result = await syncUpstream({ apply: true, run: api.run });
  assert.equal(result.status, 'synced');
  assert.equal(result.previousForkSha, OLD);
  assert.equal(result.fork.sha, NEW);
  assert.equal(result.upstream.sha, NEW);
  assert.equal(api.mutated(), true);
  assert.equal(api.calls.filter(call => call[1] === 'repo').length, 1);
});

test('already-current apply is idempotent and never invokes sync', async () => {
  const api = github({ forkSha: NEW });
  const result = await syncUpstream({ apply: true, run: api.run });
  assert.equal(result.status, 'up_to_date');
  assert.equal(api.mutated(), false);
});

test('a fork with independent commits is rejected before any write', async () => {
  const api = github({ comparison: { status: 'diverged', ahead_by: 100, behind_by: 2 } });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'FORK_DIVERGED' });
  assert.equal(api.mutated(), false);
});

test('a fork ahead of upstream cannot be overwritten', async () => {
  const api = github({ comparison: { status: 'behind', ahead_by: 0, behind_by: 1 } });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'FORK_DIVERGED' });
  assert.equal(api.mutated(), false);
});

test('wrong authenticated account cannot update the fork', async () => {
  const api = github({ login: 'someone-else' });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'ACCOUNT_MISMATCH' });
  assert.equal(api.mutated(), false);
});

test('a repository with a wrong fork parent cannot be synchronized', async () => {
  const api = github({ parent: 'other/deepseek-harness' });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'FORK_IDENTITY_MISMATCH' });
  assert.equal(api.mutated(), false);
});

test('an unexpected default branch fails closed', async () => {
  const api = github({ branch: 'custom' });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'FORK_IDENTITY_MISMATCH' });
  assert.equal(api.mutated(), false);
});

test('upstream fetch failure produces a fixed error and never leaks command stderr', async () => {
  const api = github({ failAt: 'repos/deepseek-ai/deepseek-harness/git/ref' });
  let output = '';
  const exitCode = await runCli(['--apply'], { run: api.run, write: value => { output += value; } });
  const result = JSON.parse(output);
  assert.equal(exitCode, 1);
  assert.equal(result.ok, false);
  assert.equal(result.code, 'GITHUB_COMMAND_FAILED');
  assert.equal(output.includes('secret-canary'), false);
  assert.equal(api.mutated(), false);
});

test('failed sync never reports success or exposes its raw output', async () => {
  const api = github({ failAt: 'sync' });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), error => error.code === 'GITHUB_COMMAND_FAILED' && !error.message.includes('secret-canary'));
});

test('successful command with a mismatched post-sync branch is not reported as synced', async () => {
  const api = github({ postFork: OLD });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'POST_SYNC_MISMATCH' });
});

test('an upstream race is accepted only when both live heads are equal and its version is read again', async () => {
  const api = github({ postFork: LATER, postUpstream: LATER });
  const result = await syncUpstream({ apply: true, run: api.run });
  assert.equal(result.status, 'synced');
  assert.equal(result.fork.sha, LATER);
  assert.equal(result.upstream.sha, LATER);
  assert.ok(api.calls.some(call => call.includes(`repos/deepseek-ai/deepseek-harness/contents/apps/cli/package.json?ref=${LATER}`)));
});

test('invalid compare metadata cannot be treated as safe fast-forward', async () => {
  const api = github({ comparison: { status: 'ahead', ahead_by: 100, behind_by: '0' } });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'INVALID_COMPARISON' });
  assert.equal(api.mutated(), false);
});

test('invalid upstream package metadata is rejected before sync', async () => {
  const api = github({ version: 'not-a-version' });
  await assert.rejects(syncUpstream({ apply: true, run: api.run }), { code: 'INVALID_UPSTREAM_VERSION' });
  assert.equal(api.mutated(), false);
});

test('command line defaults to read-only and refuses ambiguous or unknown flags', () => {
  assert.deepEqual(parseArgs([]), { apply: false });
  assert.deepEqual(parseArgs(['--check']), { apply: false });
  assert.deepEqual(parseArgs(['--apply']), { apply: true });
  for (const args of [['--apply', '--check'], ['--force'], ['--repo', 'other/repo'], ['--apply', '--apply']]) {
    assert.throws(() => parseArgs(args), { code: 'INVALID_ARGUMENTS' });
  }
});

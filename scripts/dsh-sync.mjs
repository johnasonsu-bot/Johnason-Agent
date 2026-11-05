#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const UPSTREAM = 'deepseek-ai/deepseek-harness';
const FORK = 'johnasonsu-bot/deepseek-harness';
const OWNER = 'johnasonsu-bot';
const BRANCH = 'master';
const SHA = /^[a-f0-9]{40}$/;
const VERSION = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/;

class SyncError extends Error {
  constructor(code, stage) {
    super(code);
    this.code = code;
    this.stage = stage;
  }
}

function fail(code, stage) { throw new SyncError(code, stage); }

// Use the existing authenticated GitHub CLI; no token is read, printed or
// persisted by this program. Capture all raw output, including failed commands.
function runGh(command, args) {
  return spawnSync(command, args, {
    encoding: 'utf8', shell: false, timeout: 120_000, maxBuffer: 2 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, GH_HOST: 'github.com', GH_PROMPT_DISABLED: '1', GH_DEBUG: '', GIT_TRACE: '', GIT_CURL_VERBOSE: '' },
  });
}

export function parseArgs(args) {
  if (args.length === 0 || (args.length === 1 && args[0] === '--check')) return { apply: false };
  if (args.length === 1 && args[0] === '--apply') return { apply: true };
  fail('INVALID_ARGUMENTS', 'arguments');
}

/** Only the fixed upstream/fork pair is permitted; local checkouts are untouched. */
export async function syncUpstream({ apply = false, run = runGh } = {}) {
  if (typeof apply !== 'boolean' || typeof run !== 'function') fail('INVALID_ARGUMENTS', 'arguments');
  async function command(args, stage) {
    let result;
    try { result = await run('gh', args); }
    catch { fail('GITHUB_COMMAND_FAILED', stage); }
    if (result?.status !== 0 || result.error || result.signal) fail('GITHUB_COMMAND_FAILED', stage);
    return result.stdout;
  }
  async function api(path, projection, stage) {
    const output = await command(['api', '--hostname', 'github.com', '--method', 'GET', path, '--jq', projection], stage);
    try { return JSON.parse(output); }
    catch { fail('INVALID_GITHUB_RESPONSE', stage); }
  }
  async function identity(repo, upstream) {
    const meta = await api(`repos/${repo}`, '{full_name,owner:{login:.owner.login},fork,parent:{full_name:.parent.full_name},default_branch}', upstream ? 'upstream_identity' : 'fork_identity');
    if (upstream) {
      if (meta?.full_name !== UPSTREAM || meta.owner?.login !== 'deepseek-ai' || meta.default_branch !== BRANCH || meta.fork !== false) fail('UPSTREAM_IDENTITY_MISMATCH', 'upstream_identity');
    } else if (meta?.full_name !== FORK || meta.owner?.login !== OWNER || meta.default_branch !== BRANCH || meta.fork !== true || meta.parent?.full_name !== UPSTREAM) {
      fail('FORK_IDENTITY_MISMATCH', 'fork_identity');
    }
  }
  async function head(repo, stage) {
    const ref = await api(`repos/${repo}/git/ref/heads/${BRANCH}`, '{ref,object:{type:.object.type,sha:.object.sha}}', stage);
    if (ref?.ref !== `refs/heads/${BRANCH}` || ref.object?.type !== 'commit' || !SHA.test(ref.object?.sha ?? '')) fail('INVALID_BRANCH_REF', stage);
    return ref.object.sha;
  }
  async function version(sha) {
    const file = await api(`repos/${UPSTREAM}/contents/apps/cli/package.json?ref=${sha}`, '{encoding,content}', 'upstream_version');
    let manifest;
    try {
      if (file?.encoding !== 'base64' || typeof file.content !== 'string' || file.content.length > 256 * 1024) throw new Error();
      manifest = JSON.parse(Buffer.from(file.content, 'base64').toString('utf8'));
    } catch { fail('INVALID_UPSTREAM_VERSION', 'upstream_version'); }
    if (manifest?.name !== '@deepseek-ai/dsh' || typeof manifest.version !== 'string' || !VERSION.test(manifest.version)) fail('INVALID_UPSTREAM_VERSION', 'upstream_version');
    return manifest.version;
  }
  const user = await api('user', '{login}', 'account');
  if (user?.login !== OWNER) fail('ACCOUNT_MISMATCH', 'account');
  await identity(UPSTREAM, true);
  await identity(FORK, false);
  const upstreamSha = await head(UPSTREAM, 'upstream_head');
  const forkSha = await head(FORK, 'fork_head');
  const upstreamVersion = await version(upstreamSha);
  const result = {
    schema: 'johnason.dsh.sync.v1', ok: true, mode: apply ? 'apply' : 'check',
    upstream: { repository: UPSTREAM, branch: BRANCH, sha: upstreamSha, version: upstreamVersion },
    fork: { repository: FORK, branch: BRANCH, sha: forkSha },
    aheadBy: 0, behindBy: 0,
  };
  if (upstreamSha === forkSha) return { ...result, status: 'up_to_date' };

  // The comparison is pinned to both observed SHAs. Only the graph counts are
  // needed; requesting one commit avoids the large/truncated commits list.
  const comparison = await api(`repos/${FORK}/compare/${forkSha}...deepseek-ai:${upstreamSha}?per_page=1&page=1`, '{status,ahead_by,behind_by}', 'comparison');
  if (!Number.isSafeInteger(comparison?.ahead_by) || comparison.ahead_by < 0
      || !Number.isSafeInteger(comparison?.behind_by) || comparison.behind_by < 0
      || !['ahead', 'behind', 'diverged', 'identical'].includes(comparison.status)) fail('INVALID_COMPARISON', 'comparison');
  if (comparison.behind_by > 0) fail('FORK_DIVERGED', 'comparison');
  if (comparison.status !== 'ahead' || comparison.ahead_by < 1) fail('INVALID_COMPARISON', 'comparison');
  result.aheadBy = comparison.ahead_by;
  result.behindBy = comparison.behind_by;
  if (!apply) return { ...result, status: 'update_available' };

  // No --force: GitHub rejects a fork that diverged since the read-only check.
  await command(['repo', 'sync', FORK, '--source', UPSTREAM, '--branch', BRANCH], 'sync');
  await identity(FORK, false);
  const afterUpstream = await head(UPSTREAM, 'post_upstream_head');
  const afterFork = await head(FORK, 'post_fork_head');
  if (afterFork !== afterUpstream) fail('POST_SYNC_MISMATCH', 'verification');
  return {
    ...result, status: 'synced', previousForkSha: forkSha,
    upstream: { ...result.upstream, sha: afterUpstream, version: afterUpstream === upstreamSha ? upstreamVersion : await version(afterUpstream) },
    fork: { ...result.fork, sha: afterFork },
  };
}

/** One JSON record and a meaningful exit status; raw errors never reach logs. */
export async function runCli(args, { run = runGh, write = value => process.stdout.write(value) } = {}) {
  try {
    const result = await syncUpstream({ ...parseArgs(args), run });
    write(JSON.stringify(result) + '\n');
    return 0;
  } catch (error) {
    const known = error instanceof SyncError;
    write(JSON.stringify({ schema: 'johnason.dsh.sync.v1', ok: false, status: 'failed', code: known ? error.code : 'UNEXPECTED_FAILURE', stage: known ? error.stage : 'internal' }) + '\n');
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await runCli(process.argv.slice(2));
}

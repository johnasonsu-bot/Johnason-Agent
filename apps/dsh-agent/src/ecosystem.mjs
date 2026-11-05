import { join, isAbsolute } from 'node:path';
import { createHash } from 'node:crypto';
import { readFile, readdir, lstat } from 'node:fs/promises';

/** Digest the shipped package only, excluding development dependencies. */
export async function subscriptionRuntimeDigest(root) {
  const hash = createHash('sha256');
  const manifest = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
  for (const key of ['devDependencies', 'scripts', 'pnpm', 'packageManager']) delete manifest[key];
  hash.update(JSON.stringify(manifest));
  async function visit(relative) {
    const path = join(root, relative), stat = await lstat(path);
    if (stat.isDirectory()) {
      for (const name of (await readdir(path)).sort()) await visit(join(relative, name));
    } else if (stat.isFile()) {
      hash.update(relative); hash.update('\0'); hash.update(await readFile(path));
    } else throw new Error('Subscription runtime may contain only regular files');
  }
  for (const name of ['lib', 'cordis.patch.yml', 'LICENSE', 'LOCAL-ADAPTER.md', 'UPSTREAM.json', 'native/keychain-helper', 'native/keychain-helper.c']) await visit(name);
  return hash.digest('hex');
}

export const COMMUNITY_PLUGINS = Object.freeze([
  { name: '@linxin666/dsh-web-all', version: '0.4.15', repository: 'https://github.com/zhu1090093659/dsh-web', revision: 'e8cc1955bab16cd6acecdefd5259d8b2531a98a2' },
  { name: 'dsh-plugin-subscriptions', version: '0.9.12-johnason.1', upstreamVersion: '0.9.12', repository: 'https://github.com/V1ki/dsh-plugin-subscriptions', revision: 'be350cd7845847b487b5ec29581452c906f4b19d', storage: 'macos-keychain' },
  { name: 'dshmarket', version: '1.73.11', repository: 'https://github.com/dsh-market/dsh-market', revision: '21648685b20d81ea049a3c2b4fe1fa6228fc16bb' },
]);

/** Non-secret, local-only defaults. Subscription OAuth remains in Keychain. */
export function ecosystemOverlay(modelIds = ['gemma4:e4b']) {
  if (!Array.isArray(modelIds) || !modelIds.length || modelIds.some(id => typeof id !== 'string' || !/^[\w][\w.:/-]{0,159}$/.test(id))) {
    throw new Error('At least one valid Ollama model ID is required');
  }
  return [
    ...['web-ui-remote-bridge', 'web-ui-task-board-remote', 'web-ui-ssh', 'web-ui-market',
      // Community Agents 0.4.15 imports a removed dsh-subagent/internal entry.
      'web-ui-agents',
      // This profile uses Codex/Ollama. Native DeepSeek account startup reads
      // credentials before the user unlocks Vault; keep Vault's strict behavior.
      'deepseek-account', 'account-controller', 'ui-settings-account'].map(id => ({ id, disabled: true })),
    { id: 'llm-subscriptions', config: { providers: ['codex'] } },
    { id: 'credentials', config: { localOllama: true } },
    { id: 'llm-pi-ai', config: { providers: { 'johnason-ollama': {
      displayName: 'Ollama 本机', api: 'openai-completions', baseURL: 'http://127.0.0.1:11434/v1',
      apiKeyEnv: 'JOHNASON_OLLAMA_LOCAL_PLACEHOLDER',
      models: [...new Set(modelIds)].map(id => ({ id, name: id, contextWindow: 32768, maxTokens: 8192 })),
      retryPolicy: { mode: 'normal', maxRetries: 1 },
    } } } },
  ];
}

/** All installations use the official profile package manager and exact package versions. */
export function ecosystemPlan({ repoRoot, dataRoot, workspace, port = 3081, runtimeKey = 'unbuilt' }) {
  for (const path of [repoRoot, dataRoot, workspace]) if (typeof path !== 'string' || !isAbsolute(path)) throw new Error('Absolute paths are required');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port');
  const cli = join(repoRoot, 'apps/dsh-agent/src/cli.mjs');
  const overlayPath = join(dataRoot, 'johnason-ecosystem.patch.json');
  const common = ['--data-dir', dataRoot, '--workspace', workspace, '--local-ollama'];
  if (runtimeKey !== 'unbuilt' && !/^[a-f0-9]{64}$/.test(runtimeKey)) throw new Error('Invalid subscription artifact digest');
  const subscriptionsSource = join(repoRoot, 'apps/dsh-agent/community/subscriptions');
  const subscriptions = join(dataRoot, 'packages', `subscriptions-${runtimeKey}`);
  const launchArgs = [cli, 'web', ...common, '--port', String(port), '--patch', overlayPath, '--no-open'];
  return {
    dataRoot, overlayPath, subscriptions, subscriptionsSource, runtimeKey, plugins: COMMUNITY_PLUGINS,
    commands: [
      [cli, 'web', ...common, '--dump-config'],
      [cli, 'plugin', ...common, '--profile', 'web', 'add', '@linxin666/dsh-web-all@0.4.15', 'dshmarket@1.73.11', '--ignore-scripts'],
      [join(subscriptionsSource, 'scripts/stage-runtime.mjs'), subscriptions],
      [cli, 'plugin', ...common, '--profile', 'web', 'add', `file:${subscriptions}`, '--ignore-scripts'],
      [cli, 'web', ...common, '--patch', overlayPath, '--dump-config'],
    ], launchArgs,
  };
}

export async function runEcosystemPlan(plan, { apply = false, run }) {
  if (!apply) return { status: 'planned', ...plan };
  for (const [index, args] of plan.commands.entries()) {
    const code = await run(args);
    if (code !== 0) throw new Error(`Ecosystem step ${index + 1} failed with exit ${code}; existing user data is retained`);
  }
  return { status: 'installed', ...plan };
}

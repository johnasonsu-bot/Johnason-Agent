#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ecosystemPlan, ecosystemOverlay, runEcosystemPlan, subscriptionRuntimeDigest } from '../src/ecosystem.mjs';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const args = process.argv.slice(2);
const options = { repoRoot, workspace: repoRoot, dataRoot: join(homedir(), '.johnason-dsh-ecosystem-20261105'), port: 3081 };
let apply = false, models = ['gemma4:e4b'];

try {
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === '--apply') { apply = true; continue; }
    if (!['--data-dir', '--workspace', '--port', '--ollama-model'].includes(arg) || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: provision-ecosystem.mjs [--apply] [--data-dir PATH] [--workspace PATH] [--port 3081] [--ollama-model MODEL]');
    const value = args[++i];
    if (arg === '--data-dir') options.dataRoot = resolve(value);
    if (arg === '--workspace') options.workspace = resolve(value);
    if (arg === '--port') options.port = Number(value);
    if (arg === '--ollama-model') models = [value];
  }
  const source = join(repoRoot, 'apps/dsh-agent/community/subscriptions');
  try { options.runtimeKey = await subscriptionRuntimeDigest(source); }
  catch (error) { if (apply) throw new Error('Build community/subscriptions first; its complete runtime artifacts are required'); }
  const plan = ecosystemPlan(options);
  const overlay = JSON.stringify(ecosystemOverlay(models), null, 2) + '\n';
  if (apply) {
    await access(join(plan.subscriptionsSource, 'lib/index.js'));
    let existingRuntime = false;
    try { await access(plan.subscriptions); existingRuntime = true; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (existingRuntime) {
      if (await subscriptionRuntimeDigest(plan.subscriptions) !== plan.runtimeKey) throw new Error('Existing subscription runtime differs; refusing to replace it');
      plan.commands = plan.commands.filter(command => !command[0].endsWith('/stage-runtime.mjs'));
    }
    const markerPath = join(plan.dataRoot, 'johnason-ecosystem.json');
    let marker;
    try { marker = JSON.parse(await readFile(markerPath, 'utf8')); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      let exists = true;
      try { await access(plan.dataRoot); } catch (error) { if (error.code === 'ENOENT') exists = false; else throw error; }
      if (exists) throw new Error('Existing data directory is not an ecosystem profile; choose a new --data-dir');
    }
    if (marker && marker.schema !== 'johnason.ecosystem.v1') throw new Error('Unknown ecosystem profile');
    try {
      const previous = await readFile(plan.overlayPath, 'utf8');
      if (previous !== overlay) throw new Error('Existing ecosystem overlay differs; preserve it and choose a new data directory');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await mkdir(plan.dataRoot, { recursive: true, mode: 0o700 });
    if (!marker) await writeFile(markerPath, JSON.stringify({ schema: 'johnason.ecosystem.v1', createdAt: new Date().toISOString() }) + '\n', { mode: 0o600, flag: 'wx' });
    await writeFile(plan.overlayPath, overlay, { mode: 0o600 });
  }
  const result = await runEcosystemPlan(plan, { apply, run: command => new Promise((resolveCode, reject) => {
    const child = spawn(process.execPath, command, { cwd: repoRoot, env: process.env,
      stdio: ['ignore', command.includes('--dump-config') ? 'ignore' : 'inherit', 'inherit'] });
    child.once('error', reject); child.once('exit', code => resolveCode(code ?? 1));
  }) });
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}

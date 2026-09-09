import { spawn } from 'node:child_process';
import { realpathSync, statSync } from 'node:fs';
import { basename } from 'node:path';
import { StringDecoder } from 'node:string_decoder';

function error(code, message) { return Object.assign(new Error(message), { code }); }
const keys = new Set(['sandbox_required', 'mode', 'network', 'workspaceRoot', 'timeoutMs', 'maxOutputBytes']);
export function validateSandboxRequirements(requirements) {
  if (!requirements || typeof requirements !== 'object' || Array.isArray(requirements)) throw error('SANDBOX_REQUIREMENT_UNSUPPORTED', 'Explicit native sandbox requirements required');
  if (Object.keys(requirements).some(key => !keys.has(key)) || requirements.sandbox_required !== true ||
    !['read-only', 'workspace-write'].includes(requirements.mode) || requirements.network !== 'host') {
    throw error('SANDBOX_REQUIREMENT_UNSUPPORTED', 'Native file confinement and network=host only; no network isolation, container or resource quotas');
  }
  const timeoutMs = requirements.timeoutMs ?? 30_000;
  const maxOutputBytes = requirements.maxOutputBytes ?? 64 * 1024;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 3_600_000 ||
    !Number.isSafeInteger(maxOutputBytes) || maxOutputBytes < 1 || maxOutputBytes > 1024 * 1024) {
    throw error('SANDBOX_REQUIREMENT_UNSUPPORTED', 'timeoutMs must be 1..3600000; maxOutputBytes must be 1..1048576');
  }
  let workspaceRoot;
  try {
    // Resolve symlink/.. using filesystem semantics, before lexical normalization.
    workspaceRoot = realpathSync.native(requirements.workspaceRoot);
    if (!statSync(workspaceRoot).isDirectory()) throw new Error('Not directory');
  } catch { throw error('SANDBOX_INVALID_WORKSPACE', 'workspaceRoot must name an existing directory'); }
  return { sandbox_required: true, mode: requirements.mode, network: 'host', workspaceRoot, timeoutMs, maxOutputBytes };
}

function argvValid(argv) { return Array.isArray(argv) && argv.length > 0 && argv.every((s, i) => typeof s === 'string' && !s.includes('\0') && (i !== 0 || s.length > 0)); }
function diagnosticScanner(rules, denialSignatures) {
  const normalized = rules.map(rule => ({ ...rule,
    fatalSignatures: rule.fatalSignatures.map(s => s.toLowerCase()),
    informationalLines: (rule.informationalLines ?? []).map(s => s.toLowerCase()),
    lineMatched: false, matched: false,
  }));
  const denials = denialSignatures.map(s => s.toLowerCase());
  const overlap = Math.max(1, ...denials.map(s => s.length), ...normalized.flatMap(rule => rule.fatalSignatures.map(s => s.length))) - 1;
  const infoLimit = Math.max(0, ...normalized.flatMap(rule => rule.informationalLines.map(s => s.length)));
  const decoder = new StringDecoder('utf8');
  let tail = '', prefix = '', lineLength = 0, denialMatched = false;
  const endLine = (newline) => {
    // Retain only enough prefix to test exact informational-line equality.
    // Long lines cannot equal any exemption, but are still fully scanned.
    const fullLine = lineLength <= infoLimit + 1 ? (newline ? prefix.replace(/\r$/, '') : prefix) : null;
    for (const rule of normalized) {
      if (rule.lineMatched && !rule.informationalLines.includes(fullLine)) rule.matched = true;
      rule.lineMatched = false;
    }
    tail = ''; prefix = ''; lineLength = 0;
  };
  const scan = decoded => {
    const lines = decoded.toLowerCase().split('\n');
    lines.forEach((part, index) => {
      const window = tail + part;
      for (const rule of normalized) {
        if (!rule.lineMatched && rule.fatalSignatures.some(s => window.includes(s))) rule.lineMatched = true;
      }
      if (!denialMatched && denials.some(s => window.includes(s))) denialMatched = true;
      tail = overlap ? window.slice(-overlap) : '';
      prefix += part.slice(0, Math.max(0, infoLimit + 1 - prefix.length));
      lineLength = Math.min(infoLimit + 2, lineLength + part.length);
      if (index < lines.length - 1) endLine(true);
    });
  };
  return {
    write(chunk) { scan(decoder.write(chunk)); },
    finish(exitCode) {
      scan(decoder.end()); endLine(false);
      const runnerFailure = Boolean(exitCode) && normalized.some(rule => rule.matched &&
        (!rule.allowedExitCodes || rule.allowedExitCodes.includes(exitCode)));
      return { runnerFailure, denied: !runnerFailure && exitCode !== 0 && denialMatched };
    },
  };
}

/** Caller owns the effect transaction. Nonzero exit is an observation, never proof of zero side effects. */
export async function runSandboxTask({ sandbox, argv, requirements, signal }) {
  const policy = validateSandboxRequirements(requirements);
  if (!argvValid(argv)) throw error('SANDBOX_INVALID_ARGV', 'argv must contain a program and literal NUL-free arguments');
  if (signal?.aborted) return { state: 'ABORTED', started: false, cancelled: true, timedOut: false, exitCode: null, stdout: '', stderr: '', backend: null, enforcement: null, outputTruncated: false, denied: false };
  if (typeof sandbox?.confine !== 'function') throw error('SANDBOX_UNAVAILABLE', 'Native sandbox provider unavailable');
  const wrapped = sandbox.confine([...argv], { mode: policy.mode, workspaceRoot: policy.workspaceRoot });
  if (!argvValid(wrapped?.argv) || !['full', 'partial'].includes(wrapped.enforcement) ||
    !Array.isArray(wrapped.denialSignatures) || !Array.isArray(wrapped.runnerFailureRules)) {
    throw error('SANDBOX_UNAVAILABLE', 'Invalid native confinement response');
  }
  const runner = wrapped.argv[0];
  // The public seam has no backend id: report observable runner identity, not private provider state.
  const backend = basename(runner) === 'sandbox-exec' ? 'native-seatbelt' : `native:${basename(runner)}`;
  const diagnostics = diagnosticScanner(wrapped.runnerFailureRules, wrapped.denialSignatures);
  return new Promise((resolve, reject) => {
    const child = spawn(runner, wrapped.argv.slice(1), { cwd: policy.workspaceRoot, shell: false,
      detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = Buffer.alloc(0), stderr = Buffer.alloc(0), outputTruncated = false;
    let timedOut = false, cancelled = false, started = false, spawnError;
    let killTimer;
    const capture = (previous, chunk) => {
      const remaining = policy.maxOutputBytes - previous.length;
      if (chunk.length > remaining) outputTruncated = true;
      return Buffer.concat([previous, chunk.subarray(0, remaining)]);
    };
    child.stdout.on('data', data => { stdout = capture(stdout, data); });
    child.stderr.on('data', data => { diagnostics.write(data); stderr = capture(stderr, data); });
    const kill = sig => {
      try { if (process.platform === 'win32') child.kill(sig); else if (child.pid) process.kill(-child.pid, sig); }
      catch (failure) { if (failure.code !== 'ESRCH') spawnError ??= failure; }
    };
    const stop = () => {
      kill('SIGTERM');
      killTimer ??= setTimeout(() => kill('SIGKILL'), 200);
    };
    const abort = () => { cancelled = true; stop(); };
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => { timedOut = true; stop(); }, policy.timeoutMs);
    child.on('spawn', () => { started = true; if (signal?.aborted) abort(); });
    child.on('error', failure => { spawnError = failure; });
    child.on('close', (exitCode, exitSignal) => {
      // The leader may exit on TERM while a same-group descendant ignores it and
      // has closed its pipes. Do not cancel escalation before killing that group.
      if (timedOut || cancelled || exitSignal || spawnError) kill('SIGKILL');
      clearTimeout(timer); clearTimeout(killTimer); signal?.removeEventListener('abort', abort);
      if (spawnError && !started) return reject(error('SANDBOX_UNAVAILABLE', 'Confined runner could not start; command was not run unconfined'));
      // Do not flush a trailing partial code point into a three-byte replacement
      // character, which could exceed the configured byte bound.
      const out = new StringDecoder('utf8').write(stdout), err = new StringDecoder('utf8').write(stderr);
      const { runnerFailure, denied } = diagnostics.finish(exitCode);
      resolve({ state: timedOut || cancelled || exitSignal || spawnError || runnerFailure ? 'UNKNOWN' : 'COMMITTED',
        started, exitCode, exitSignal, stdout: out, stderr: err, backend, enforcement: wrapped.enforcement,
        network: 'host', timedOut, cancelled, outputTruncated, denied, runnerFailure,
        ...(runnerFailure ? { errorCode: 'SANDBOX_UNAVAILABLE' } : {}) });
    });
  });
}

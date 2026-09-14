import { createRequire } from 'node:module';
import { isDeepStrictEqual } from 'node:util';
import { StringDecoder } from 'node:string_decoder';
import { VaultStore } from './vault-store.mjs';
import { installVaultUi } from './vault-ui.mjs';

const require = createRequire(new URL('../../../third_party/deepseek-harness/apps/cli/package.json', import.meta.url));
const baseRequire = createRequire(require.resolve('@deepseek-ai/dsh-base'));
const localRequire = createRequire(baseRequire.resolve('@deepseek-ai/dsh-credentials-local'));
const { CredentialProvider, credentialRef, parseCredentialKey } = await import(localRequire.resolve('@deepseek-ai/dsh-credentials'));
const { Service } = await import(require.resolve('@deepseek-ai/cordis'));

function jsonValue(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number' && Number.isFinite(value)) return;
  if (typeof value !== 'object' || seen.has(value)) throw new TypeError('Record must contain JSON values');
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) throw new TypeError('Record must contain JSON objects');
  seen.add(value);
  for (const item of Object.values(value)) jsonValue(item, seen);
  if (Object.getOwnPropertySymbols(value).length || (Array.isArray(value) && Object.keys(value).length !== value.length)) throw new TypeError('Record must contain JSON values');
  seen.delete(value);
}

function validateRecord(record) {
  jsonValue(record);
  if (!record || Array.isArray(record) || typeof record !== 'object') throw new TypeError('Invalid credential record');
  if (record.kind === 'grant' && Object.hasOwn(record, 'payload') && Object.keys(record).every(k => ['kind', 'payload'].includes(k))) return;
  if (record.kind !== 'api-key' || Object.keys(record).some(k => !['kind', 'key', 'env'].includes(k))) throw new TypeError('Invalid credential record');
  if (record.key !== undefined && (typeof record.key !== 'string' || record.key.length === 0)) throw new TypeError('Invalid credential record');
  if (record.env !== undefined) {
    if (!record.env || Array.isArray(record.env) || typeof record.env !== 'object') throw new TypeError('Invalid credential record');
    for (const [ref, value] of Object.entries(record.env)) {
      credentialRef(ref);
      if (typeof value !== 'string') throw new TypeError('Invalid credential record');
    }
  }
}

/** Encrypted-only native credentials provider. Every operation rereads the vault. */
export default class VaultCredentials extends CredentialProvider {
  constructor(ctx, config) {
    super(ctx);
    if (!config?.path || !['web', 'headless'].includes(config.mode)) throw new Error('Vault path and mode are required');
    this.config = config;
    this.vault = new VaultStore(config.path);
  }

  async* [Service.init]() {
    yield () => this.vault.lock();
    if (this.config.mode === 'web') {
      this.ctx.inject(['webServer'], ctx => installVaultUi(ctx, this.vault));
    } else {
      await unlockTerminal(this.vault);
    }
  }

  async resolve(ref) {
    credentialRef(ref);
    const value = (await this.vault.read()).refs[ref];
    return typeof value === 'string' && value.length ? { value, source: 'vault' } : undefined;
  }
  async describe(ref) {
    credentialRef(ref);
    if ((await this.vault.status()).locked) return { configured: false, writable: false };
    const resolved = await this.resolve(ref);
    return { configured: Boolean(resolved), ...resolved && { source: 'vault' }, writable: true };
  }
  async set(ref, value) {
    credentialRef(ref);
    if (typeof value !== 'string' || !value.length) throw new TypeError('Credential value must be non-empty');
    let changed = false;
    await this.vault.update(data => { changed = data.refs[ref] !== value; Object.defineProperty(data.refs, ref, { value, enumerable: true, writable: true, configurable: true }); return data; });
    if (changed) this.notifyUpdated(ref);
  }
  async unset(ref) {
    credentialRef(ref);
    let changed = false;
    await this.vault.update(data => { changed = Object.hasOwn(data.refs, ref); delete data.refs[ref]; return data; });
    if (changed) this.notifyUpdated(ref);
  }
  async readRecord(key) {
    parseCredentialKey(key);
    const record = (await this.vault.read()).records[key];
    if (record !== undefined) validateRecord(record);
    return record;
  }
  async describeRecord(key) {
    parseCredentialKey(key);
    if ((await this.vault.status()).locked) return { configured: false, writable: false };
    const record = await this.readRecord(key);
    return { configured: record !== undefined, ...record && { kind: record.kind }, writable: true };
  }
  async listRecords() {
    if ((await this.vault.status()).locked) return [];
    return Object.entries((await this.vault.read()).records).map(([key, record]) => {
      parseCredentialKey(key); validateRecord(record); return { key, kind: record.kind };
    });
  }
  async modifyRecord(key, mutate) {
    parseCredentialKey(key);
    let result, changed = false;
    await this.vault.update(async data => {
      const current = data.records[key];
      if (current !== undefined) validateRecord(current);
      const replacement = await mutate(structuredClone(current));
      if (replacement !== undefined) {
        validateRecord(replacement);
        changed = !isDeepStrictEqual(current, replacement);
        data.records[key] = structuredClone(replacement);
      }
      result = data.records[key];
      return data;
    });
    if (changed) this.notifyRecordUpdated(key);
    return result;
  }
  async deleteRecord(key) {
    parseCredentialKey(key);
    let changed = false;
    await this.vault.update(data => { changed = Object.hasOwn(data.records, key); delete data.records[key]; return data; });
    if (changed) this.notifyRecordUpdated(key);
  }
}

/** Read a master password without echo; never accept arguments or redirected input. */
export async function passwordPrompt(label) {
  const input = process.stdin, output = process.stderr;
  if (!input.isTTY || !output.isTTY) throw new Error('Vault is locked. Use the local Web /vault unlock page; headless requires its own interactive terminal unlock.');
  output.write(label);
  const wasRaw = input.isRaw;
  input.setRawMode(true); input.resume();
  return new Promise((resolve, reject) => {
    const decoder = new StringDecoder('utf8');
    let value = '';
    const finish = (error) => {
      input.off('data', onData); input.setRawMode(wasRaw); input.pause(); output.write('\n');
      decoder.end();
      if (error) reject(error); else resolve(value);
      value = '';
    };
    const onData = chunk => {
      for (const char of decoder.write(chunk)) {
        if (char === '\u0003' || char === '\u0004') { finish(new Error('Unlock cancelled')); return; }
        if (char === '\r' || char === '\n') { finish(); return; }
        if (char === '\u007f' || char === '\b') value = value.slice(0, -1);
        else if (char >= ' ' && value.length < 4096) value += char;
      }
    };
    input.on('data', onData);
  });
}

async function unlockTerminal(vault) {
  const status = await vault.status();
  let password = await passwordPrompt(status.initialized ? 'Vault master password: ' : 'Create vault master password: ');
  try {
    if (!password) throw new Error('Master password must not be empty');
    if (status.initialized) await vault.unlock(password);
    else {
      let confirmation = await passwordPrompt('Confirm master password: ');
      if (password !== confirmation) throw new Error('Passwords do not match');
      confirmation = '';
      await vault.initialize(password);
    }
  } finally { password = ''; }
}

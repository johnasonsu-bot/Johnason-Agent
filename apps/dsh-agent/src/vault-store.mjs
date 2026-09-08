import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
  scrypt as nodeScrypt,
} from 'node:crypto';
import { mkdir, open, readFile, rename, stat, unlink } from 'node:fs/promises';
import { dirname } from 'node:path';
import { promisify } from 'node:util';

import { acquireVaultLock } from './vault-lock.mjs';

const scrypt = promisify(nodeScrypt);
const VERSION = 1;
const KDF = Object.freeze({ name: 'scrypt', N: 16_384, r: 8, p: 1 });

function vaultError(code, message, cause) {
  const error = new Error(message, cause ? { cause } : undefined);
  error.code = code;
  return error;
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function decodeBase64(value, length = null) {
  if (typeof value !== 'string' || value.length === 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
    throw vaultError('VAULT_FORMAT_ERROR', 'Invalid vault envelope');
  }
  const buffer = Buffer.from(value, 'base64');
  if ((length !== null && buffer.length !== length) || buffer.toString('base64') !== value) {
    throw vaultError('VAULT_FORMAT_ERROR', 'Invalid vault envelope');
  }
  return buffer;
}

function parseEnvelope(contents) {
  let envelope;
  try {
    envelope = JSON.parse(contents);
  } catch (cause) {
    throw vaultError('VAULT_FORMAT_ERROR', 'Vault is not valid JSON', cause);
  }
  if (!isObject(envelope)
      || envelope.version !== VERSION
      || !isObject(envelope.kdf)
      || envelope.kdf.name !== KDF.name
      || envelope.kdf.N !== KDF.N
      || envelope.kdf.r !== KDF.r
      || envelope.kdf.p !== KDF.p) {
    throw vaultError('VAULT_FORMAT_ERROR', 'Unsupported vault envelope');
  }
  return {
    salt: decodeBase64(envelope.salt, 16),
    nonce: decodeBase64(envelope.nonce, 12),
    tag: decodeBase64(envelope.tag, 16),
    ciphertext: decodeBase64(envelope.ciphertext),
  };
}

function parseData(plaintext) {
  let data;
  try {
    data = JSON.parse(plaintext);
  } catch (cause) {
    throw vaultError('VAULT_FORMAT_ERROR', 'Vault plaintext is malformed', cause);
  }
  if (!isObject(data) || !isObject(data.refs) || !isObject(data.records)) {
    throw vaultError('VAULT_FORMAT_ERROR', 'Vault data has an invalid shape');
  }
  return data;
}

async function deriveKey(password, salt) {
  if (typeof password !== 'string' && !Buffer.isBuffer(password)) {
    throw new TypeError('password must be a string or Buffer');
  }
  const passwordBytes = Buffer.from(password);
  try {
    return await scrypt(passwordBytes, salt, 32, KDF);
  } finally {
    passwordBytes.fill(0);
  }
}

function decrypt(envelope, key, failureCode) {
  try {
    const decipher = createDecipheriv('aes-256-gcm', key, envelope.nonce);
    decipher.setAuthTag(envelope.tag);
    return Buffer.concat([decipher.update(envelope.ciphertext), decipher.final()]);
  } catch (cause) {
    throw vaultError(failureCode, 'Vault authentication failed', cause);
  }
}

function encrypt(data, key, salt) {
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  const plaintext = Buffer.from(JSON.stringify(data));
  try {
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    return JSON.stringify({
      version: VERSION,
      kdf: KDF,
      salt: salt.toString('base64'),
      nonce: nonce.toString('base64'),
      tag: cipher.getAuthTag().toString('base64'),
      ciphertext: ciphertext.toString('base64'),
    });
  } finally {
    plaintext.fill(0);
  }
}

async function atomicWrite(path, contents, beforeCommit = () => {}) {
  const temporary = `${path}.tmp-${process.pid}-${randomBytes(12).toString('hex')}`;
  let handle;
  try {
    handle = await open(temporary, 'wx', 0o600);
    await handle.writeFile(contents, 'utf8');
    await handle.sync();
    await handle.close();
    handle = undefined;
    beforeCommit();
    await rename(temporary, path);
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    await unlink(temporary).catch(() => {});
    throw error;
  }
}

export class VaultStore {
  #path;
  #key = null;
  #generation = 0;

  constructor(path) {
    if (typeof path !== 'string' || path.length === 0) throw new TypeError('path must be a non-empty string');
    this.#path = path;
  }

  async status() {
    let initialized;
    try {
      initialized = (await stat(this.#path)).isFile();
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      initialized = false;
    }
    return { initialized, locked: this.#key === null };
  }

  async initialize(password) {
    const generation = this.#generation;
    await mkdir(dirname(this.#path), { recursive: true, mode: 0o700 });
    const release = await acquireVaultLock(this.#path);
    let key;
    try {
      this.#assertGeneration(generation);
      try {
        await stat(this.#path);
        throw vaultError('VAULT_EXISTS', 'Vault is already initialized');
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      const salt = randomBytes(16);
      key = await deriveKey(password, salt);
      this.#assertGeneration(generation);
      await atomicWrite(
        this.#path,
        encrypt({ refs: {}, records: {} }, key, salt),
        () => this.#assertGeneration(generation),
      );
      this.#assertGeneration(generation);
      this.#replaceKey(key);
      key = null;
    } finally {
      if (key) key.fill(0);
      await release();
    }
  }

  async unlock(password) {
    const generation = this.#generation;
    const envelope = parseEnvelope(await this.#readContents());
    const key = await deriveKey(password, envelope.salt);
    try {
      const plaintext = decrypt(envelope, key, 'VAULT_UNLOCK_FAILED');
      try {
        parseData(plaintext.toString('utf8'));
      } finally {
        plaintext.fill(0);
      }
      this.#assertGeneration(generation);
      this.#replaceKey(key);
    } catch (error) {
      key.fill(0);
      throw error;
    }
  }

  lock() {
    if (this.#key) this.#key.fill(0);
    this.#key = null;
    this.#generation += 1;
  }

  async read() {
    const { key, generation } = this.#access();
    const envelope = parseEnvelope(await this.#readContents());
    if (this.#key !== key || this.#generation !== generation) throw vaultError('VAULT_LOCKED', 'Vault is locked');
    const plaintext = decrypt(envelope, key, 'VAULT_DECRYPT_FAILED');
    try {
      return parseData(plaintext.toString('utf8'));
    } finally {
      plaintext.fill(0);
    }
  }

  async update(mutator) {
    if (typeof mutator !== 'function') throw new TypeError('mutator must be a function');
    const access = this.#access();
    const release = await acquireVaultLock(this.#path);
    try {
      this.#assertAccess(access);
      const envelope = parseEnvelope(await this.#readContents());
      const plaintext = decrypt(envelope, access.key, 'VAULT_DECRYPT_FAILED');
      let data;
      try {
        data = parseData(plaintext.toString('utf8'));
      } finally {
        plaintext.fill(0);
      }
      await mutator(data);
      this.#assertAccess(access);
      if (!isObject(data.refs) || !isObject(data.records)) {
        throw vaultError('VAULT_FORMAT_ERROR', 'Mutator produced invalid vault data');
      }
      await atomicWrite(
        this.#path,
        encrypt(data, access.key, envelope.salt),
        () => this.#assertAccess(access),
      );
    } finally {
      await release();
    }
  }

  #access() {
    if (!this.#key) throw vaultError('VAULT_LOCKED', 'Vault is locked');
    return { key: this.#key, generation: this.#generation };
  }

  #assertAccess(access) {
    if (this.#key !== access.key || this.#generation !== access.generation) {
      throw vaultError('VAULT_LOCKED', 'Vault is locked');
    }
  }

  #assertGeneration(generation) {
    if (this.#generation !== generation) throw vaultError('VAULT_LOCKED', 'Vault is locked');
  }

  #replaceKey(key) {
    if (this.#key) this.#key.fill(0);
    this.#key = key;
    this.#generation += 1;
  }

  async #readContents() {
    try {
      return await readFile(this.#path, 'utf8');
    } catch (cause) {
      if (cause.code === 'ENOENT') throw vaultError('VAULT_NOT_INITIALIZED', 'Vault is not initialized', cause);
      throw cause;
    }
  }
}

import { mkdir, readFile, rmdir, unlink, writeFile } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';

function vaultError(code, message, cause) {
  const error = new Error(message, cause ? { cause } : undefined);
  error.code = code;
  return error;
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

export async function acquireVaultLock(vaultPath, { timeoutMs = 5_000 } = {}) {
  const lockPath = `${vaultPath}.lock`;
  const ownerPath = `${lockPath}/owner`;
  const token = randomBytes(24).toString('hex');
  const deadline = Date.now() + timeoutMs;

  while (true) {
    try {
      await mkdir(lockPath, { mode: 0o700 });
      try {
        await writeFile(ownerPath, token, { encoding: 'utf8', flag: 'wx', mode: 0o600 });
      } catch (error) {
        await rmdir(lockPath).catch(() => {});
        throw error;
      }
      break;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      if (Date.now() >= deadline) {
        throw vaultError('VAULT_BUSY', 'Vault is busy');
      }
      await wait(15 + Math.floor(Math.random() * 20));
    }
  }

  let released = false;
  return async function release() {
    if (released) return;
    const owner = await readFile(ownerPath, 'utf8').catch(() => null);
    if (owner !== token) {
      throw vaultError('VAULT_LOCK_OWNERSHIP_LOST', 'Vault lock ownership was lost');
    }
    await unlink(ownerPath);
    await rmdir(lockPath);
    released = true;
  };
}

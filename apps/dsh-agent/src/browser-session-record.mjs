import { isDeepStrictEqual } from 'node:util';

export const BROWSER_SESSION_RECORD_KEY = 'client-connection/browser-session';
const roots = new WeakMap();

function validate(record) {
  if (!record || record.kind !== 'grant' || Object.keys(record).some(key => !['kind', 'payload'].includes(key))
    || !record.payload || record.payload.version !== 1
    || Object.keys(record.payload).some(key => !['version', 'secret'].includes(key))
    || typeof record.payload.secret !== 'string'
    || !/^[A-Za-z0-9_-]{43}$/.test(record.payload.secret)
    || Buffer.from(record.payload.secret, 'base64url').toString('base64url') !== record.payload.secret) {
    throw new TypeError('Invalid browser signing record');
  }
}

/** Keep native browser bootstrap authentication available before the vault is unlocked.
 * The record is scoped to one application root, survives plugin reloads, and never touches disk.
 * A fresh process requires its new native launch URL; provider keys and system grants remain in the vault.
 */
export function browserSessionRecord(root) {
  if (roots.has(root)) return roots.get(root);
  let record;
  let pending = Promise.resolve();
  const enqueue = operation => {
    const result = pending.then(operation);
    pending = result.then(() => {}, () => {});
    return result;
  };
  const store = {
    read: () => enqueue(() => structuredClone(record)),
    modify: mutate => enqueue(async () => {
      const replacement = await mutate(structuredClone(record));
      let changed = false;
      if (replacement !== undefined) {
        validate(replacement);
        changed = !isDeepStrictEqual(record, replacement);
        record = structuredClone(replacement);
      }
      return { record: structuredClone(record), changed };
    }),
    clear: () => enqueue(() => {
      const changed = record !== undefined;
      record = undefined;
      return changed;
    }),
  };
  roots.set(root, store);
  return store;
}

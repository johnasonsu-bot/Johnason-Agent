import { nativeRequire, nativeUrl } from './runtime.mjs';

const require = nativeRequire();
export const { Service } = await import(require.resolve('@deepseek-ai/cordis'));
export const { createUserMessage } = await import(nativeUrl('packages/llm/llm/lib/index.js'));
// Preserve a source host's ESM identity when it supplies aliases; the standard
// launcher and plain Node hosts resolve the built native CLI dependency graph.
export const { KNOWN_SESSION_EVENT_TYPES } = await import('@deepseek-ai/dsh-session').catch(cause => {
  if (cause.code !== 'ERR_MODULE_NOT_FOUND' || !cause.message.includes("package '@deepseek-ai/dsh-session'")) throw cause;
  return import(require.resolve('@deepseek-ai/dsh-session'));
});

const retainedTypes = new Map();

/**
 * Compatibility shim for the pinned reader, not a stable upstream registration API.
 * This enables current-format required events only; it does not change legacy
 * migration vocabularies or make historical event hashes safe to rewrite.
 */
export function retainRequiredEventTypes(types) {
  const catalog = KNOWN_SESSION_EVENT_TYPES;
  if (!(catalog instanceof Set) || Object.getPrototypeOf(catalog) !== Set.prototype
      || ['has', 'add', 'delete'].some(method => catalog[method] !== Set.prototype[method])) {
    throw Object.assign(new Error('Pinned native event catalog is no longer the supported Set implementation'), { code: 'MEMORY_NATIVE_EVENT_CATALOG_UNSUPPORTED' });
  }
  const selected = [...new Set(types)];
  if (selected.some(type => typeof type !== 'string' || !type)) throw new TypeError('Required event types must be nonempty strings');
  for (const type of selected) {
    let retained = retainedTypes.get(type);
    if (!retained) {
      retained = { users: 0, added: !catalog.has(type) };
      retainedTypes.set(type, retained);
      if (retained.added) catalog.add(type);
    }
    retained.users++;
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    for (const type of selected) {
      const retained = retainedTypes.get(type);
      if (--retained.users === 0) {
        if (retained.added) catalog.delete(type);
        retainedTypes.delete(type);
      }
    }
  };
}

/**
 * Bridge the pinned Session read API for the existing synchronous memory hooks.
 * Upstream marks snapshotEvents deprecated; a future projection/persistence
 * adapter must replace this seam before that API disappears. Never treat a
 * missing snapshot as an empty history, which could bypass activation guards.
 */
export function snapshotSessionEvents(session, from = 0, to = undefined) {
  if (typeof session?.snapshotEvents !== 'function') {
    throw Object.assign(new Error('Pinned native Session snapshot API is unavailable'), { code: 'MEMORY_NATIVE_SESSION_API_UNSUPPORTED' });
  }
  const events = session.snapshotEvents(from, to);
  if (!Array.isArray(events)) {
    throw Object.assign(new Error('Pinned native Session snapshot is not an event array'), { code: 'MEMORY_NATIVE_SESSION_API_UNSUPPORTED' });
  }
  return events;
}

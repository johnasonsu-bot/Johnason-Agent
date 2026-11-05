/** Run the upstream suite with in-memory credentials; retain test fixtures. */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { resolve, sep } from 'node:path';
// The user's workspace policy retains local files. Test-only teardown retains
// generated fixtures; no production code imports this runner.
function retainedPath(path) {
  const resolved = resolve(String(path));
  const allowed = [resolve(tmpdir()), '/private/tmp', '/tmp'];
  if (!allowed.some(root => resolved.startsWith(root + sep))) throw new Error('Test cleanup outside temporary fixture root refused');
  return resolved + '.retained-' + randomUUID();
}
fs.rmSync = (path) => {
  try { fs.renameSync(path, retainedPath(path)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
};
fsp.rm = async (path) => {
  try { await fsp.rename(path, retainedPath(path)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
};
syncBuiltinESMExports();
const root = new URL('../../', import.meta.url);
const { fixtureBackend } = await import(new URL('lib-test/test/local/memory-backend.js', root));
const { withSessionStorage } = await import(new URL('lib-test/src/auth/secret-storage.js', root));
const specs = fs.readdirSync(new URL('lib-test/test/', root)).filter(name => name.endsWith('.spec.js')).sort();
await withSessionStorage(fixtureBackend, async () => {
  for (const name of specs) await import(new URL(`lib-test/test/${name}`, root));
});

/** Create an immutable fresh runtime copy without source-tree devDependencies. */
import { mkdir, readFile, writeFile, readdir, lstat, copyFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve, join, relative, sep } from 'node:path';
const source = fileURLToPath(new URL('../', import.meta.url));
const target = process.argv[2] && resolve(process.argv[2]);
if (!target) throw new Error('Usage: node scripts/stage-runtime.mjs <new-absolute-runtime-directory>');
if (target === resolve(source) || !relative(source, target).startsWith('..' + sep)) {
  throw new Error('Runtime stage must be outside the source package, so its devDependencies cannot shadow platform peers');
}
try { await lstat(target); throw new Error('Runtime destination already exists; select a new directory'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
async function copyTree(from, to) {
  const info = await lstat(from);
  if (info.isDirectory()) {
    await mkdir(to, { mode: 0o755 });
    for (const entry of await readdir(from)) await copyTree(join(from, entry), join(to, entry));
  } else if (info.isFile()) await copyFile(from, to);
  else throw new Error('Runtime source must contain regular files and directories only');
}
const manifest = JSON.parse(await readFile(join(source, 'package.json'), 'utf8'));
for (const key of ['devDependencies', 'scripts', 'pnpm', 'packageManager']) delete manifest[key];
await mkdir(target, { recursive: true });
for (const name of ['lib', 'cordis.patch.yml', 'LICENSE', 'LOCAL-ADAPTER.md', 'UPSTREAM.json']) {
  await copyTree(join(source, name), join(target, name));
}
await mkdir(join(target, 'native'));
for (const name of ['keychain-helper', 'keychain-helper.c']) {
  await copyFile(join(source, 'native', name), join(target, 'native', name));
}
await writeFile(join(target, 'package.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(JSON.stringify({ path: target, version: manifest.version, credentialsIncluded: false, devDependenciesIncluded: false }));

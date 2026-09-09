import { createRequire } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Compose native bundles and user patches, with the encrypted provider as the final overlay. */
export async function prepareProfile(config) {
  const require = createRequire(join(config.upstreamRoot, 'apps/cli/package.json'));
  const boot = await import(require.resolve('@deepseek-ai/dsh-app-boot'));
  const args = [...(config.invocation?.args ?? config.args)];
  let profile = config.invocation?.profile ?? (config.mode === 'web' ? 'web' : 'headless');
  if (!config.invocation && (config.mode === 'web' || config.mode === 'plugin')) args.shift();
  const patchFiles = [...config.invocation?.patches ?? []];
  for (let i = 0; !config.invocation && i < args.length; i++) {
    if (args[i] === '--') break;
    if (args[i] === '--profile' || args[i] === '--patch') {
      const flag = args[i], value = args[i + 1];
      if (!value || value.startsWith('--')) throw new Error(`${flag} requires a value`);
      if (flag === '--profile') profile = value; else patchFiles.push(value);
      args.splice(i, 2); i--;
    }
  }
  boot.resolveProfileDir(profile, config.dataRoot);
  if (config.mode === 'plugin') return { profile, args, patchFiles };
  const anchor = join(config.upstreamRoot, 'apps/cli/package.json');
  const pluginPath = fileURLToPath(new URL('./credentials-plugin.mjs', import.meta.url));
  const bundleName = '@johnason/dsh-encrypted-base';
  const bundleDir = join(config.dataRoot, 'profiles/node_modules', bundleName);
  const baseDir = boot.resolveBundleDir('johnason-dsh', '@deepseek-ai/dsh-base', anchor, config.dataRoot);
  const baseManifest = JSON.parse(await readFile(join(baseDir, 'package.json'), 'utf8'));
  const basePatch = await readFile(join(baseDir, baseManifest.dsh.bundle.patch), 'utf8');
  const needle = "name: '@deepseek-ai/dsh-credentials-local'";
  if (basePatch.split(needle).length !== 2) throw new Error('Native base must have exactly one credential provider');
  await mkdir(bundleDir, { recursive: true, mode: 0o700 });
  await writeFile(join(bundleDir, 'package.json'), JSON.stringify({ name: bundleName, private: true, dsh: { bundle: { patch: './cordis.patch.yml' } } }) + '\n');
  await writeFile(join(bundleDir, 'cordis.patch.yml'), basePatch.replace(needle, `name: ${JSON.stringify(pluginPath)}`));
  // Native patch names are match guards, not replacements. Substitute only the base bundle slot.
  const profileDir = boot.resolveProfileDir(profile, config.dataRoot);
  let loaded = boot.loadProfile('johnason-dsh', profile, anchor, config.dataRoot);
  const manifest = boot.readProfileManifest('johnason-dsh', profileDir);
  if (manifest.dsh?.profile?.bundles?.includes('@deepseek-ai/dsh-base')) {
    manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.map(name => name === '@deepseek-ai/dsh-base' ? bundleName : name);
    boot.writeProfileManifest(profileDir, manifest);
    loaded = boot.loadProfile('johnason-dsh', profile, anchor, config.dataRoot);
  }
  const memoryPath = fileURLToPath(new URL('./memory-recovery-plugin.mjs', import.meta.url));
  const overlay = [{ id: 'credentials', config: { path: join(config.dataRoot, 'vault.enc'), mode: config.mode } },
    { insert: [{ id: 'memory-recovery', name: memoryPath, config: { path: join(config.dataRoot, 'memory/recovery.sqlite'), enabled: true } }] }];
  const layers = [loaded.layers.flatMap(layer => layer.patches), loaded.patches,
    boot.loadOptionalPatches('johnason-dsh', join(config.dataRoot, 'cordis.patch.yml')) ?? [],
    ...patchFiles.map(path => boot.loadOverlayPatches('johnason-dsh', path)), overlay];
  const rows = boot.composeEntries(layers);
  const providers = rows.filter(row => row.id === 'credentials' || /credentials-(?:local|plugin)/.test(row.name ?? ''));
  if (providers.length !== 1 || providers[0].name !== pluginPath || providers[0].disabled) throw new Error('Exactly one encrypted credential provider is required');
  const overlayPath = join(config.dataRoot, `standalone-${profile}.patch.yml`);
  await mkdir(config.dataRoot, { recursive: true, mode: 0o700 });
  await writeFile(overlayPath, JSON.stringify(overlay, null, 2) + '\n', { mode: 0o600 });
  return { profile, args, rows, overlayPath, patchFiles: [...patchFiles, overlayPath] };
}

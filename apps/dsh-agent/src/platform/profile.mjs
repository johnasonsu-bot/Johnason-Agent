import { mkdir, readFile, writeFile, copyFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createPluginPlan } from '../plugins/index.mjs';
import { loadProfilePlatform } from './launch-platform.mjs';

/** Compose native bundles and user patches, with the encrypted provider as the final overlay. */
export async function prepareProfile(config, options = {}) {
  const platform = options.platform ?? await loadProfilePlatform(config.upstreamRoot);
  const { boot } = platform;
  const plugins = options.plugins ?? createPluginPlan(config);
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
  if (config.invocation?.fromDefaultProfile !== undefined) {
    // Initialize before loadProfile can create the target. The later native
    // boot/dump reuses this encrypted profile and must not initialize it twice.
    await platform.initializeProfileFromDefault(profile, config.invocation.fromDefaultProfile, config.dataRoot);
  }
  if (plugins.contractVersion !== 1) throw new Error('Unsupported local plugin contract');
  const { anchor } = platform;
  const pluginPath = plugins.credential.entry;
  const bundleName = plugins.credential.bundleName;
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
  const overlay = plugins.profileOverlay(platform);
  for (const bundle of plugins.clientBundles) {
    // The native runtime resolver excludes arbitrary packages in the shared
    // profiles/node_modules directory; the owning profile has its own scope.
    const uiDir = join(profileDir, 'node_modules', bundle.name);
    await mkdir(uiDir, { recursive: true, mode: 0o700 });
    for (const file of bundle.files) await copyFile(new URL(file, bundle.sourceRoot), join(uiDir, file));
  }
  const layers = [loaded.layers.flatMap(layer => layer.patches), loaded.patches,
    boot.loadOptionalPatches('johnason-dsh', join(config.dataRoot, 'cordis.patch.yml')) ?? [],
    ...patchFiles.map(path => boot.loadOverlayPatches('johnason-dsh', path)), overlay];
  const rows = boot.composeEntries(layers);
  const providers = rows.filter(row => row.id === 'credentials' || /credentials-(?:local|plugin)/.test(row.name ?? ''));
  // Native file-backed profile layers canonicalize absolute paths to file URLs.
  const providerNames = [pluginPath, pathToFileURL(pluginPath).href];
  if (providers.length !== 1 || !providerNames.includes(providers[0].name) || providers[0].disabled) throw new Error('Exactly one encrypted credential provider is required');
  const overlayPath = join(config.dataRoot, `standalone-${profile}.patch.yml`);
  await mkdir(config.dataRoot, { recursive: true, mode: 0o700 });
  await writeFile(overlayPath, JSON.stringify(overlay, null, 2) + '\n', { mode: 0o600 });
  return { profile, args, rows, overlayPath, patchFiles: [...patchFiles, overlayPath] };
}

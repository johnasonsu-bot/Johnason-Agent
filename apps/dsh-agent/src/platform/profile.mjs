import { mkdir, readFile, writeFile, copyFile, lstat, rename } from 'node:fs/promises';
import { join, dirname, basename } from 'node:path';
import { randomUUID } from 'node:crypto';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { createPluginPlan } from '../plugins/index.mjs';
import { loadProfilePlatform } from './launch-platform.mjs';

async function realDirectory(path) {
  try { await mkdir(path, { mode: 0o700 }); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`Generated plugin directory must not be a link: ${path}`);
}

async function preserveLinkedEntry(path, profileDir) {
  let info;
  try { info = await lstat(path); }
  catch (error) { if (error.code === 'ENOENT') return; throw error; }
  if (!info.isSymbolicLink() && !(info.isFile() && info.nlink > 1)) return;
  const backups = join(profileDir, '.johnason-preserved-package-links');
  await realDirectory(backups);
  await rename(path, join(backups, `${basename(path)}-${randomUUID()}`));
}

async function privateBundleDirectory(profileDir, name) {
  await mkdir(profileDir, { recursive: true, mode: 0o700 });
  const modules = join(profileDir, 'node_modules');
  await realDirectory(modules);
  const target = join(modules, name);
  if (dirname(target) !== modules) await realDirectory(dirname(target));
  // pnpm can replace a generated package with a symlink to its source. Preserve
  // that link before materializing our copy; never copy or patch through it.
  await preserveLinkedEntry(target, profileDir);
  await realDirectory(target);
  return target;
}

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
    const target = boot.resolveProfileDir(profile, config.dataRoot);
    try {
      await lstat(target);
      throw new Error(`Profile ${profile} already exists; preserve its files and select a new profile name`);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await platform.initializeProfileFromDefault(profile, config.invocation.fromDefaultProfile, config.dataRoot);
  }
  if (plugins.contractVersion !== 1) throw new Error('Unsupported local plugin contract');
  const { anchor } = platform;
  const pluginPath = plugins.credential.entry;
  const bundleName = plugins.credential.bundleName;
  const bundleDir = join(config.dataRoot, 'profiles/node_modules', bundleName);
  const baseDir = boot.resolveBundleDir('johnason-dsh', '@deepseek-ai/dsh-base', anchor, config.dataRoot);
  const baseManifest = JSON.parse(await readFile(join(baseDir, 'package.json'), 'utf8'));
  const localManifest = JSON.parse(await readFile(new URL('../../package.json', import.meta.url), 'utf8'));
  const basePatch = await readFile(join(baseDir, baseManifest.dsh.bundle.patch), 'utf8');
  const needle = "name: '@deepseek-ai/dsh-credentials-local'";
  if (basePatch.split(needle).length !== 2) throw new Error('Native base must have exactly one credential provider');
  await mkdir(bundleDir, { recursive: true, mode: 0o700 });
  await writeFile(join(bundleDir, 'package.json'), JSON.stringify({ name: bundleName, version: localManifest.version, private: true, dsh: { bundle: { patch: './cordis.patch.yml' } } }) + '\n');
  await writeFile(join(bundleDir, 'cordis.patch.yml'), basePatch.replace(needle, `name: ${JSON.stringify(pluginPath)}`));
  // Native patch names are match guards, not replacements. Substitute only the base bundle slot.
  const profileDir = boot.resolveProfileDir(profile, config.dataRoot);
  const provisionedKey = 'johnason:provisioned-bundles';
  let previous = {};
  try { previous = JSON.parse(await readFile(join(profileDir, 'package.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const provisioned = previous[provisionedKey] ?? [];
  if (!Array.isArray(provisioned) || provisioned.some(name => typeof name !== 'string')) throw new Error('Invalid local bundle provisioning record');
  for (const bundle of plugins.clientBundles) {
    const uninstalled = provisioned.includes(bundle.name)
      && !Object.hasOwn(previous.dependencies ?? {}, bundle.name)
      && !previous.dsh?.profile?.bundles?.includes(bundle.name);
    if (uninstalled) continue;
    // Profile-local packages are resolved by both the runtime and the native manager.
    const uiDir = await privateBundleDirectory(profileDir, bundle.name);
    for (const file of bundle.files) {
      const destination = join(uiDir, file);
      await preserveLinkedEntry(destination, profileDir);
      await copyFile(new URL(file, bundle.sourceRoot), destination);
    }
    if (bundle.bundlePatches?.length) {
      // Implementation paths belong to the inherited bundle; a final overlay
      // would make the entire entry read-only to native live Settings edits.
      const path = join(uiDir, 'cordis.patch.yml');
      const patches = boot.loadOverlayPatches('johnason-dsh', path);
      await writeFile(path, JSON.stringify([...patches, ...bundle.bundlePatches], null, 2) + '\n');
    }
  }
  let loaded = boot.loadProfile('johnason-dsh', profile, anchor, config.dataRoot);
  const manifest = boot.readProfileManifest('johnason-dsh', profileDir);
  let manifestChanged = false;
  if (manifest.dsh?.profile?.bundles?.includes('@deepseek-ai/dsh-base')) {
    manifest.dsh.profile.bundles = manifest.dsh.profile.bundles.map(name => name === '@deepseek-ai/dsh-base' ? bundleName : name);
    manifestChanged = true;
  }
  for (const bundle of plugins.clientBundles) {
    if (provisioned.includes(bundle.name)) continue;
    if (!Object.hasOwn(manifest.dependencies ?? {}, bundle.name)) {
      manifest.dependencies = { ...manifest.dependencies, [bundle.name]: `file:${fileURLToPath(bundle.sourceRoot)}` };
      manifest.dsh ??= {};
      manifest.dsh.profile ??= {};
      manifest.dsh.profile.bundles = [...new Set([...manifest.dsh.profile.bundles ?? [], bundle.name])];
      manifestChanged = true;
    }
    // Native package operations preserve extra manifest fields. This separates
    // first upgrade from an explicit later uninstall without recreating files.
    manifest[provisionedKey] = [...new Set([...manifest[provisionedKey] ?? [], bundle.name])];
    manifestChanged = true;
  }
  if (manifestChanged) {
    boot.writeProfileManifest(profileDir, manifest);
    loaded = boot.loadProfile('johnason-dsh', profile, anchor, config.dataRoot);
  }
  const overlay = plugins.profileOverlay(platform);
  // Native headless now disables its ambient credential provider. Johnason must
  // retain the encrypted provider; user overrides are still validated afterward.
  const modePatches = config.mode === 'headless' ? [{ id: 'credentials', disabled: false }] : [];
  const layers = [loaded.layers.flatMap(layer => layer.patches), modePatches, loaded.patches,
    boot.loadOptionalPatches('johnason-dsh', join(config.dataRoot, 'cordis.patch.yml')) ?? [],
    ...patchFiles.map(path => boot.loadOverlayPatches('johnason-dsh', path)), overlay];
  const rows = boot.composeEntries(layers);
  const providers = rows.filter(row => row.id === 'credentials' || /credentials-(?:local|plugin)/.test(row.name ?? ''));
  // Native file-backed profile layers canonicalize absolute paths to file URLs.
  const providerNames = [pluginPath, pathToFileURL(pluginPath).href];
  if (providers.length !== 1 || !providerNames.includes(providers[0].name) || providers[0].disabled) throw new Error('Exactly one encrypted credential provider is required');
  const overlayPath = join(config.dataRoot, `standalone-${profile}.patch.yml`);
  await mkdir(config.dataRoot, { recursive: true, mode: 0o700 });
  await writeFile(overlayPath, JSON.stringify([...modePatches, ...overlay], null, 2) + '\n', { mode: 0o600 });
  return { profile, args, rows, overlayPath, patchFiles: [...patchFiles, overlayPath] };
}

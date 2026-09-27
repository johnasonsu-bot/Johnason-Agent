import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseProviders, readProviders } from './systems/systems.mjs';
import { prepareTrustedCore, closeTrustedCores, CORE_ENV_KEYS } from './systems/dataplatform.mjs';

const entry = path => fileURLToPath(new URL(path, import.meta.url));

/** Local extension contract v1. Creating a plan never opens a Vault or business database. */
export function createPluginPlan(config) {
  const providers = config.providers === undefined ? readProviders(config.systemsPath, config.corePath) : parseProviders(config.providers);
  // Public plugin paths are persisted in user profile name guards. Keep those
  // stable forwarding entries while implementations live under this directory.
  const credential = Object.freeze({ id: 'credentials', entry: entry('../credentials-plugin.mjs'),
    implementationEntry: entry('./credentials/credentials-plugin.mjs'),
    bundleName: '@johnason/dsh-encrypted-base' });
  const clientBundles = config.mode === 'web' ? [{ name: '@johnason/dsh-memory-ui',
    sourceRoot: new URL('../../browser/', import.meta.url), files: ['package.json', 'index.mjs', 'client.js', 'cordis.patch.yml'] },
    { name: '@johnason/dsh-model-api', sourceRoot: new URL('../../model-api-browser/', import.meta.url),
      files: ['package.json', 'index.mjs', 'client.js', 'cordis.patch.yml'] }] : [];
  // This bundle contributes host tools and a runtime Skill in every profile;
  // its optional browser half is loaded only by native Web clients.
  clientBundles.push({ name: '@johnason/dsh-local-models', sourceRoot: new URL('../../local-model-browser/', import.meta.url),
    files: ['package.json', 'index.mjs', 'client.js', 'cordis.patch.yml'] });
  return {
    contractVersion: 1, credential, clientBundles, providers,
    profileOverlay({ authorizationEntry }) {
      const overlay = [{ id: credential.id, config: { path: join(config.dataRoot, 'vault.enc'), mode: config.mode } },
        { insert: [{ id: 'memory-recovery', name: entry('../memory-recovery-plugin.mjs'),
          config: { path: join(config.dataRoot, 'memory/recovery.sqlite'), enabled: true } }] }];
      if (providers.length) {
        if (typeof authorizationEntry !== 'string' || !authorizationEntry) throw new Error('Platform authorization service is required');
        overlay.push({ insert: [{ id: 'dataplatform-authorization', name: authorizationEntry },
          { id: 'dataplatform', name: entry('../systems-plugin.mjs'), config: { providers } }] });
      }
      // A config-only patch never inserts a disabled bundle's row. Keep it in
      // the overlay so the native manager can re-enable the UI without restart.
      if (config.mode === 'web') {
        overlay.push({ id: 'memory-ui', config: { providers: providers.map(({ id, label, type }) => ({ id, label, type })) } });
      }
      overlay.push({ id: 'local-models', config: { implementation: entry('./local-models/plugin.mjs') } });
      return overlay;
    },
    async prepare() {
      // The platform calls this before taking any native launch-environment snapshot.
      try { for (const provider of providers) if (provider.type === 'dataplatform') prepareTrustedCore(provider.corePath); }
      finally { for (const key of CORE_ENV_KEYS) delete process.env[key]; }
    },
    dispose: closeTrustedCores,
  };
}

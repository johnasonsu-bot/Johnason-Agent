import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepareProfile } from './platform/profile.mjs';
import { loadLaunchPlatform } from './platform/launch-platform.mjs';
import { createPluginPlan } from './plugins/index.mjs';

/** Composition root: platform owns upstream compatibility; plugins own local lifecycle. */
export async function runNative(config, dependencies = {}) {
  const platform = dependencies.platform ?? await loadLaunchPlatform(config.upstreamRoot);
  config.invocation = platform.parseArgs(config.args);
  const plugins = dependencies.plugins ?? createPluginPlan(config);
  const compose = dependencies.prepareProfile ?? prepareProfile;
  const prepared = await compose(config, { plugins });
  if (config.invocation.mode === 'dump-config') {
    await platform.runDumpConfig(prepared.profile, config.invocation.defaultOnly, prepared.patchFiles);
    return 0;
  }
  try {
    await plugins.prepare();
    if (config.mode === 'plugin') {
      const status = await platform.runPlugin(prepared.profile, prepared.args);
      await plugins.dispose();
      return status;
    }
    const environment = await platform.environment(process.env);
    const { ctx } = await platform.runProfile({ ...prepared, environment });
    // Root ownership survives plugin reload; disposal never belongs to a transient plugin.
    if (ctx.get('loader') === undefined) await plugins.dispose();
    else ctx.effect(() => () => plugins.dispose());
    return 0;
  } catch (error) {
    await plugins.dispose();
    throw error;
  }
}

// This process receives only the launcher's allowlisted environment.
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await runNative(JSON.parse(process.argv[2]));
}

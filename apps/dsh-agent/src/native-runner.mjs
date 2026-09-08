import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { prepareProfile } from './profile.mjs';

// This process receives only the launcher's allowlisted environment. No native bin/.env loader is imported.
const config = JSON.parse(process.argv[2]);
const require = createRequire(join(config.upstreamRoot, 'apps/cli/package.json'));
const { parseDshArgs } = await import(pathToFileURL(join(config.upstreamRoot, 'apps/cli/src/args.ts')));
const invocation = parseDshArgs(config.args, '0.1.1-rc.2');
config.invocation = invocation;
const prepared = await prepareProfile(config);
if (config.mode === 'plugin') {
  const { runPlugin } = await import(pathToFileURL(join(config.upstreamRoot, 'apps/cli/src/plugin.ts')));
  process.exitCode = runPlugin(prepared.profile, prepared.args);
} else if (invocation.mode === 'dump-config') {
  const { runDumpConfig } = await import(pathToFileURL(join(config.upstreamRoot, 'apps/cli/src/dump-config.ts')));
  runDumpConfig(prepared.profile, invocation.defaultOnly, prepared.patchFiles);
} else {
  const { createLaunchEnvironmentSnapshot } = await import(require.resolve('@deepseek-ai/dsh-launch-environment'));
  const { runProfile } = await import(pathToFileURL(join(config.upstreamRoot, 'apps/cli/src/profile-boot.ts')));
  await runProfile({ ...prepared, environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: process.env }]) });
}

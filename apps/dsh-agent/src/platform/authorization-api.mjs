import { nativeRequire, nativeUrl } from './runtime.mjs';

/** Native authorization services are loaded only for the standalone system CLI. */
export async function loadAuthorizationPlatform() {
  const require = nativeRequire();
  const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
  const { default: Authorization } = await import(nativeUrl('packages/credentials/authorization/lib/index.js'));
  return { Context, Authorization };
}

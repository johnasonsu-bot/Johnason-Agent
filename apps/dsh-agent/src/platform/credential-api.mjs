import { createRequire } from 'node:module';
import { nativeRequire } from './runtime.mjs';

// Resolve the same credential provider class used by the native base plugin.
const require = nativeRequire();
const baseRequire = createRequire(require.resolve('@deepseek-ai/dsh-base'));
const localRequire = createRequire(baseRequire.resolve('@deepseek-ai/dsh-credentials-local'));
export const { CredentialProvider, credentialRef, parseCredentialKey } = await import(localRequire.resolve('@deepseek-ai/dsh-credentials'));
const { Context, Service } = await import(require.resolve('@deepseek-ai/cordis'));
export { Service };

/** Preserve the native application's root identity across derived contexts. */
export function rootContext(ctx) {
  return ctx[Context.root];
}

// Web's tsx aliases resolve the native LLM module from source. Preserve that
// class identity for the adapter's instanceof guard; plain Node uses its build.
export const { LlmError } = await import('@deepseek-ai/dsh-llm').catch(cause => {
  if (cause.code !== 'ERR_MODULE_NOT_FOUND' || !cause.message.includes("package '@deepseek-ai/dsh-llm'")) throw cause;
  return import(require.resolve('@deepseek-ai/dsh-llm'));
});

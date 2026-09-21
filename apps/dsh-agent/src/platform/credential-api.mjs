import { createRequire } from 'node:module';
import { nativeRequire } from './runtime.mjs';

// Resolve the same credential provider class used by the native base plugin.
const require = nativeRequire();
const baseRequire = createRequire(require.resolve('@deepseek-ai/dsh-base'));
const localRequire = createRequire(baseRequire.resolve('@deepseek-ai/dsh-credentials-local'));
export const { CredentialProvider, credentialRef, parseCredentialKey } = await import(localRequire.resolve('@deepseek-ai/dsh-credentials'));
export const { Service } = await import(require.resolve('@deepseek-ai/cordis'));

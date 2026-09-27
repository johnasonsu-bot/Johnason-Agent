import { nativeRequire } from './runtime.mjs';

// Keep native package resolution inside the platform compatibility boundary.
const require = nativeRequire();
export const { default: Schema } = await import(require.resolve('@deepseek-ai/schemastery'));

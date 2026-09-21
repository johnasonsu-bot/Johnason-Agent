// Source-only stand-ins for Cordis registration; credential and browser logic run unchanged.
import { fileURLToPath } from 'node:url';
export const createRequire = () => ({
  resolve(specifier) {
    if (!['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-credentials-local', '@deepseek-ai/dsh-credentials', '@deepseek-ai/cordis'].includes(specifier)) throw new Error(`Unexpected native source dependency: ${specifier}`);
    return fileURLToPath(import.meta.url);
  },
});
export const Service = { init: Symbol('init') };
export const credentialRef = value => value;
export const parseCredentialKey = value => value;
export const credentialKey = (namespace, name) => `${namespace}/${name}`;
export class CredentialProvider {
  constructor(ctx) { this.ctx = ctx; }
  notifyUpdated(ref) { this.ctx.updates.push(ref); }
  notifyRecordUpdated(key) { this.ctx.updates.push(key); }
}

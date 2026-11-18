import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { nativeRequire } from './runtime.mjs';

// These pins preserve the verified community combination; the full installed
// dependency graph must still pass the separate ecosystem lock verification.
export const COMMUNITY_DEPENDENCY_PINS = Object.freeze({
  '@linxin666/dsh-client-ui-community-plugins': '0.4.18',
  '@linxin666/dsh-client-ui-preset-center': '0.4.18',
  '@linxin666/dsh-client-ui-skin-center': '0.4.18',
  '@linxin666/dsh-pet': '0.4.18',
  marked: '18.2.0',
});

function isMapping(value) {
  return value !== null && typeof value === 'object'
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}

function overrideTargetName(selector) {
  // Match pnpm 11's parsePkgAndParentSelector/parseWantedDependency rules:
  // range operators after @ are not parent delimiters; a scoped name keeps @.
  const trimmed = selector.trim();
  const parentDelimiter = trimmed.search(/[^ |@]>/);
  const target = parentDelimiter === -1 ? trimmed : trimmed.slice(parentDelimiter + 2).trim();
  const versionDelimiter = target.indexOf('@', 1);
  return versionDelimiter === -1 ? target : target.slice(0, versionDelimiter);
}

/** Apply only after the official CLI has initialized the selected web profile. */
export async function pinCommunityDependencies(dataRoot) {
  const path = join(dataRoot, 'profiles/web/pnpm-workspace.yaml');
  const source = await readFile(path, 'utf8');
  // Resolve the official dependency lazily so planning needs no native install.
  const yaml = nativeRequire()('js-yaml');
  let workspace;
  try { workspace = yaml.load(source); }
  catch (error) { throw new Error(`Invalid workspace YAML: ${path}`, { cause: error }); }
  if (!isMapping(workspace)) throw new Error(`Invalid workspace mapping: ${path}`);
  const overrides = Object.hasOwn(workspace, 'overrides') ? workspace.overrides : {};
  if (!isMapping(overrides) || Object.entries(overrides).some(([name, value]) => !name.trim() || typeof value !== 'string' || !value.trim())) {
    throw new Error(`Invalid workspace overrides: ${path}`);
  }
  for (const [selector, value] of Object.entries(overrides)) {
    const name = overrideTargetName(selector);
    if (Object.hasOwn(COMMUNITY_DEPENDENCY_PINS, name) && value !== COMMUNITY_DEPENDENCY_PINS[name]) {
      throw new Error(`Community dependency override conflict for ${selector}: expected ${COMMUNITY_DEPENDENCY_PINS[name]}`);
    }
  }
  if (Object.keys(COMMUNITY_DEPENDENCY_PINS).every(name => Object.hasOwn(overrides, name))) return;
  // pnpm 11 reads overrides here, rather than package.json.pnpm.overrides.
  workspace.overrides = { ...overrides, ...COMMUNITY_DEPENDENCY_PINS };
  await writeFile(path, yaml.dump(workspace, { lineWidth: -1 }));
}

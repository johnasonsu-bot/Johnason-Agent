import { readFileSync } from "node:fs";

const SIDECAR_ROOT = "mvp/sidecars/deepseek-harness";
export const PLATFORM_RELEASE_PATH = "mvp/src/workbench/runtime/deepseek_harness/platform-release.json";
const RELEASE_FIELDS = ["adapterContract", "packageManager", "revision", "schema", "version"];

function exactKeys(value, keys) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === [...keys].sort().join(",");
}

export function validatePlatformRelease(value) {
  if (!exactKeys(value, RELEASE_FIELDS)
      || value.schema !== "johnason.dsh.platform.v1"
      || typeof value.revision !== "string" || !/^[0-9a-f]{40}$/.test(value.revision)
      || typeof value.version !== "string" || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/.test(value.version)
      || typeof value.packageManager !== "string" || !/^pnpm@\d+\.\d+\.\d+$/.test(value.packageManager)
      || value.adapterContract !== 1) {
    throw new Error("DSH platform release is missing, invalid or incompatible");
  }
  return Object.freeze({ ...value });
}

export function loadPlatformRelease(url) {
  try {
    return validatePlatformRelease(JSON.parse(readFileSync(url, "utf8")));
  } catch {
    throw new Error("DSH platform release is missing, invalid or incompatible");
  }
}

export const PLATFORM_RELEASE = loadPlatformRelease(new URL(
  "../../../src/workbench/runtime/deepseek_harness/platform-release.json", import.meta.url,
));

export function validateComponentManifest(value) {
  if (!exactKeys(value, ["schema", "modules"])
      || value.schema !== "johnason.dsh.host-components.v1"
      || !Array.isArray(value.modules) || value.modules.length === 0
      || value.modules.some(name => typeof name !== "string" || !/^[a-z]+(?:-[a-z]+)*$/.test(name))
      || value.modules.join(",") !== [...new Set(value.modules)].sort().join(",")
      || ["bootstrap", "checkpoint", "event-mapper", "grant-channel", "native-session", "platform-contract", "platform-runtime", "server"]
        .some(name => !value.modules.includes(name))) {
    throw new Error("DSH component manifest is missing or invalid");
  }
  return Object.freeze({ schema: value.schema, modules: Object.freeze([...value.modules]) });
}

export function componentFiles(value) {
  const { modules } = validateComponentManifest(value);
  return Object.freeze({
    sourceEntries: Object.freeze(modules.map(name => `${name}.ts`)),
    sourceFiles: Object.freeze([
      "package.json", "tsconfig.json", "cordis.host-v2.yml", "scripts/build.mjs", "component-manifest.json",
      ...modules.map(name => `src/${name}.ts`),
    ].map(relative => `${SIDECAR_ROOT}/${relative}`).concat(PLATFORM_RELEASE_PATH)),
    artifactFiles: Object.freeze([
      ...modules.map(name => `${name}.mjs`), "deepseek-harness-host-v2.mjs", "deepseek-harness-model-host-v2.mjs",
    ].sort().map(name => `${SIDECAR_ROOT}/dist/${name}`)),
  });
}

// Execution guarantees required by Host v2, separate from public runtime features.
export const NATIVE_ADAPTER_CAPABILITIES = Object.freeze({
  orderedInput: true, transientStream: true, abortSettlement: true, platformToolCallback: true,
});

export function validateNativeAdapter(adapter) {
  if (!exactKeys(adapter, ["contractVersion", "capabilities", "run"])
      || adapter.contractVersion !== PLATFORM_RELEASE.adapterContract
      || typeof adapter.run !== "function"
      || !exactKeys(adapter.capabilities, Object.keys(NATIVE_ADAPTER_CAPABILITIES))
      || Object.keys(NATIVE_ADAPTER_CAPABILITIES).some(key => adapter.capabilities[key] !== true)) {
    throw new Error("DSH native adapter contract is invalid or incompatible");
  }
  return adapter;
}

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import test from "node:test";
import {
  PLATFORM_RELEASE, NATIVE_ADAPTER_CAPABILITIES, validatePlatformRelease,
  validateComponentManifest, validateNativeAdapter, componentFiles,
} from "../src/platform-contract.ts";

const sidecar = new URL("../", import.meta.url);
const components = JSON.parse(readFileSync(new URL("component-manifest.json", sidecar)));

test("one shared release pins the adapter and official source", () => {
  const shared = JSON.parse(readFileSync(new URL(
    "../../../src/workbench/runtime/deepseek_harness/platform-release.json", import.meta.url,
  )));
  assert.deepEqual(PLATFORM_RELEASE, shared);
  assert.equal(PLATFORM_RELEASE.adapterContract, 1);
  assert.ok(Object.isFrozen(PLATFORM_RELEASE));
});

test("release validation rejects missing, unknown and incompatible fields", () => {
  for (const field of Object.keys(PLATFORM_RELEASE)) {
    const value = { ...PLATFORM_RELEASE };
    delete value[field];
    assert.throws(() => validatePlatformRelease(value), /platform release/);
  }
  for (const patch of [
    { revision: "HEAD" }, { version: "latest" }, { packageManager: "pnpm" },
    { adapterContract: 2 }, { adapterContract: true }, { unexpected: true },
  ]) assert.throws(() => validatePlatformRelease({ ...PLATFORM_RELEASE, ...patch }), /platform release/);
});

test("component inventory covers every source and artifact without arbitrary paths", () => {
  assert.deepEqual(validateComponentManifest(components), components);
  const files = componentFiles(components);
  assert.deepEqual(files.sourceEntries, readdirSync(new URL("src/", sidecar)).sort());
  assert.ok(files.sourceFiles.includes("mvp/src/workbench/runtime/deepseek_harness/platform-release.json"));
  assert.ok(files.sourceFiles.includes("mvp/sidecars/deepseek-harness/component-manifest.json"));
  for (const name of ["platform-runtime", "platform-contract"]) {
    assert.ok(files.artifactFiles.includes(`mvp/sidecars/deepseek-harness/dist/${name}.mjs`));
  }
  for (const modules of [[], [...components.modules, "../escape"], [...components.modules, components.modules[0]], components.modules.slice().reverse()]) {
    assert.throws(() => validateComponentManifest({ ...components, modules }), /component manifest/);
  }
  assert.throws(() => validateComponentManifest({ ...components, extra: true }), /component manifest/);
});

test("production upstream imports enter only through platform-runtime", () => {
  let officialImports = 0;
  for (const name of readdirSync(new URL("src/", sidecar))) {
    const text = readFileSync(new URL(`src/${name}`, sidecar), "utf8");
    for (const match of text.matchAll(/(?:from\s*|import\s*\(?)["']([^"']+)["']/g)) {
      if (!/third_party\/deepseek-harness|^@deepseek-ai\/|^cordis$/.test(match[1])) continue;
      assert.equal(name, "platform-runtime.ts", `${name} escapes the platform boundary`);
      officialImports++;
    }
  }
  assert.ok(officialImports > 0);
});

test("Host adapter requires the complete versioned execution contract", () => {
  const adapter = { contractVersion: 1, capabilities: { ...NATIVE_ADAPTER_CAPABILITIES }, run: async () => ({ content: "" }) };
  assert.equal(validateNativeAdapter(adapter), adapter);
  for (const patch of [{ contractVersion: 2 }, { run: null }, { capabilities: {} }, { extra: true }]) {
    assert.throws(() => validateNativeAdapter({ ...adapter, ...patch }), /native adapter/);
  }
  for (const field of Object.keys(NATIVE_ADAPTER_CAPABILITIES)) {
    assert.throws(() => validateNativeAdapter({ ...adapter, capabilities: { ...adapter.capabilities, [field]: false } }), /native adapter/);
  }
});

import { createHash } from "node:crypto";
import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = path.join(root, "src");
const outputRoot = path.join(root, "dist");
const repositoryPrefix = "mvp/sidecars/deepseek-harness";
const expectedSourceEntries = [
  "bootstrap.ts",
  "checkpoint.ts",
  "event-mapper.ts",
  "grant-channel.ts",
  "server.ts",
];
const expectedArtifactEntries = [
  "bootstrap.mjs",
  "build-receipt.json",
  "checkpoint.mjs",
  "deepseek-harness-host-v2.mjs",
  "event-mapper.mjs",
  "grant-channel.mjs",
  "server.mjs",
];


function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}


function digest(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}


async function fileRecord(relative) {
  const payload = await readFile(path.join(root, relative));
  return {
    path: `${repositoryPrefix}/${relative}`,
    sha256: createHash("sha256").update(payload).digest("hex"),
    size: payload.length,
  };
}

const sourceEntries = await readdir(sourceRoot, { withFileTypes: true });
if (sourceEntries.some(entry => !entry.isFile())
    || sourceEntries.map(entry => entry.name).sort().join(",") !== expectedSourceEntries.join(",")) {
  throw new Error("DeepSeek Harness sidecar source file set drift");
}

await rm(outputRoot, { recursive: true, force: true });
await mkdir(outputRoot, { recursive: true });

for (const entry of expectedSourceEntries) {
  const source = await readFile(path.join(sourceRoot, entry), "utf8");
  const executable = source.replaceAll(/(from\s+["'][^"']+)\.ts(["'])/g, "$1.mjs$2");
  const output = entry.replace(/\.ts$/, ".mjs");
  await writeFile(path.join(outputRoot, output), executable, "utf8");
}

await writeFile(
  path.join(outputRoot, "deepseek-harness-host-v2.mjs"),
  [
    "#!/usr/bin/env node",
    'import { readFixedPreset } from "./bootstrap.mjs";',
    'import { readPreopenedGrantChannel } from "./grant-channel.mjs";',
    'import { createSidecar, serveNdjson } from "./server.mjs";',
    "",
    'readFixedPreset(new URL("../cordis.host-v2.yml", import.meta.url));',
    "if (process.argv.length !== 2) throw new Error(\"DSH Host v2 rejects argv configuration\");",
    `const instanceDigest = "${"b".repeat(64)}";`,
    "const sidecar = createSidecar({",
    "  grantChannel: readPreopenedGrantChannel(3, instanceDigest),",
    '  runtimeId: "dsh",',
    '  buildId: "dsh:fixed-host-v2-smoke",',
    "  instanceDigest,",
    "});",
    "serveNdjson(sidecar);",
    "",
  ].join("\n"),
  { encoding: "utf8", mode: 0o755 },
);

const sourceFiles = [
  "package.json",
  "tsconfig.json",
  "cordis.host-v2.yml",
  "scripts/build.mjs",
  "src/bootstrap.ts",
  "src/checkpoint.ts",
  "src/event-mapper.ts",
  "src/grant-channel.ts",
  "src/server.ts",
];
const artifactFiles = [
  "dist/bootstrap.mjs",
  "dist/checkpoint.mjs",
  "dist/deepseek-harness-host-v2.mjs",
  "dist/event-mapper.mjs",
  "dist/grant-channel.mjs",
  "dist/server.mjs",
];
const sources = await Promise.all(sourceFiles.map(fileRecord));
const artifacts = await Promise.all(artifactFiles.map(fileRecord));
const receipt = canonical({
  schema: "workbench.runtime.dsh.host_v2_build_receipt.v1",
  command: "npm run build",
  source_digest: digest(sources),
  artifact_digest: digest(artifacts),
  artifacts,
});
await writeFile(
  path.join(outputRoot, "build-receipt.json"),
  `${JSON.stringify(receipt)}\n`,
  "utf8",
);
const artifactEntries = await readdir(outputRoot, { withFileTypes: true });
if (artifactEntries.some(entry => !entry.isFile())
    || artifactEntries.map(entry => entry.name).sort().join(",") !== expectedArtifactEntries.join(",")) {
  throw new Error("DeepSeek Harness sidecar dist file set drift");
}

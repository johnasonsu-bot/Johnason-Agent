import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PLATFORM_RELEASE, componentFiles } from "../src/platform-contract.ts";


const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = path.join(root, "src");
const outputRoot = path.join(root, "dist");
const repositoryRoot = path.resolve(root, "../../..");
const components = JSON.parse(await readFile(path.join(root, "component-manifest.json"), "utf8"));
const { sourceEntries: expectedSourceEntries, sourceFiles, artifactFiles } = componentFiles(components);
const expectedArtifactEntries = [...artifactFiles.map(relative => path.basename(relative)), "build-receipt.json"].sort();
const upstreamRoot = path.join(repositoryRoot, "third_party/deepseek-harness");
const upstreamPackage = JSON.parse(await readFile(path.join(upstreamRoot, "package.json"), "utf8"));
const upstreamCli = JSON.parse(await readFile(path.join(upstreamRoot, "apps/cli/package.json"), "utf8"));
const upstreamRevision = execFileSync("git", ["-C", upstreamRoot, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
if (upstreamRevision !== PLATFORM_RELEASE.revision || upstreamCli.version !== PLATFORM_RELEASE.version
    || upstreamPackage.packageManager !== PLATFORM_RELEASE.packageManager) {
  throw new Error("DSH platform release does not match the official checkout");
}


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
  const payload = await readFile(path.join(repositoryRoot, relative));
  return {
    path: relative,
    sha256: createHash("sha256").update(payload).digest("hex"),
    size: payload.length,
  };
}

const sourceEntries = await readdir(sourceRoot, { withFileTypes: true });
if (sourceEntries.some(entry => !entry.isFile())
    || sourceEntries.map(entry => entry.name).sort().join(",") !== expectedSourceEntries.join(",")) {
  throw new Error("DeepSeek Harness sidecar source file set drift");
}

await mkdir(outputRoot, { recursive: true });

for (const entry of expectedSourceEntries) {
  const source = await readFile(path.join(sourceRoot, entry), "utf8");
  const executable = source.replaceAll(/(from\s+["'][^"']+)\.ts(["'])/g, "$1.mjs$2");
  const output = entry.replace(/\.ts$/, ".mjs");
  await writeFile(path.join(outputRoot, output), executable, "utf8");
}

for (const [entrypoint, buildId] of [
  ["deepseek-harness-host-v2.mjs", "dsh:fixed-host-v2-smoke"],
  ["deepseek-harness-model-host-v2.mjs", "dsh:model-host-v2-r1"],
]) {
await writeFile(
  path.join(outputRoot, entrypoint),
  [
    "#!/usr/bin/env node",
    'import { readFixedPreset } from "./bootstrap.mjs";',
    'import { startPreopenedGrantReceiver } from "./grant-channel.mjs";',
    'import { createSidecar, serveNdjson } from "./server.mjs";',
    "",
    'readFixedPreset(new URL("../cordis.host-v2.yml", import.meta.url));',
    "if (process.argv.length !== 2) throw new Error(\"DSH Host v2 rejects argv configuration\");",
    'const runtimeId = "dsh";',
    `const buildId = ${JSON.stringify(buildId)};`,
    'const descriptor = process.env.WORKBENCH_PROVIDER_GRANT_FD ?? "";',
    "delete process.env.WORKBENCH_PROVIDER_GRANT_FD;",
    "if (!/^[0-9]+$/.test(descriptor)) throw new Error(\"DSH Provider Grant descriptor is missing\");",
    "const receiver = startPreopenedGrantReceiver(Number(descriptor), { runtimeId, buildId });",
    "const sidecar = createSidecar({",
    "  grantChannel: receiver.channel,",
    "  runtimeId,",
    "  buildId,",
    "});",
    "serveNdjson(sidecar, process.stdin, process.stdout, receiver.ready);",
    "",
  ].join("\n"),
  { encoding: "utf8", mode: 0o755 },
);
}

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

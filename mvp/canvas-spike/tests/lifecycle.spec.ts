import { _electron as electron, expect, test } from "@playwright/test";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

async function writeLivenessBackend(
  testDir: string,
  handshakeDelayMs = 150,
  invalidHandshake = false,
  shutdownDelayMs = 0,
  exitOnStdinEof = true,
  unexpectedExitDelayMs: number | null = null,
): Promise<string> {
  await mkdir(testDir, { recursive: true });
  const executable = path.join(testDir, "liveness-backend.mjs");
  await writeFile(executable, `#!/usr/bin/env node
import { appendFileSync, mkdirSync } from "node:fs";
import http from "node:http";
import path from "node:path";

const runtimeDir = process.argv[process.argv.indexOf("--runtime-dir") + 1];
mkdirSync(runtimeDir, { recursive: true });
const events = path.join(runtimeDir, "backend-events.log");
const record = (event) => appendFileSync(events, event + "\\n");
record("started");
let bootstrap = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { bootstrap += chunk; });
process.stdin.once("end", () => {
  record("stdin-eof");
  if (${exitOnStdinEof}) process.exit(0);
});
process.on("SIGTERM", () => {
  record("sigterm");
  setTimeout(() => { record("exited"); process.exit(0); }, ${shutdownDelayMs});
});
setTimeout(() => {
  const { instance_id } = JSON.parse(bootstrap);
  const server = http.createServer((request, response) => {
    if (request.url === "/api/health") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ status: "ok", service: "hermes-workbench", instance_id }));
      return;
    }
    response.statusCode = 404;
    response.end();
  });
  server.listen(0, "127.0.0.1", () => {
    const { port } = server.address();
    record("ready");
    process.stdout.write(JSON.stringify(${invalidHandshake ? '{ service: "wrong-service", instance_id, port }' : '{ service: "hermes-workbench", instance_id, port }'}) + "\\n");
    if (${unexpectedExitDelayMs ?? "null"} !== null) {
      setTimeout(() => {
        record("unexpected-exit");
        server.close(() => { record("exited"); process.exit(17); });
      }, ${unexpectedExitDelayMs ?? 0});
    }
  });
}, ${handshakeDelayMs});
`);
  await chmod(executable, 0o755);
  return executable;
}

async function eventsText(events: string): Promise<string> {
  try { return await readFile(events, "utf8"); } catch { return ""; }
}

test("backend liveness pipe stays open through normal startup", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"));
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: {
      ...process.env,
      HERMES_PYTHON: executable,
      HERMES_RUNTIME_DIR: runtimeDir,
    },
  });

  try {
    await app.firstWindow();
    await expect.poll(async () => eventsText(events)).toContain("ready");
    await expect.poll(async () => eventsText(events)).not.toContain("stdin-eof");
  } finally {
    await app.close();
  }
});

test("startup and activate share one backend launch", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"), 1_500);
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: { ...process.env, HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: runtimeDir },
  });

  try {
    await expect.poll(async () => eventsText(events)).toContain("started");
    await app.evaluate(({ app: electronApp }) => electronApp.emit("activate"));
    await app.firstWindow();
    await expect.poll(async () => (await eventsText(events)).match(/^started$/gm)?.length ?? 0).toBe(1);
    await app.evaluate(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));
    await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)).toBe(1);
  } finally {
    try { await app.close(); } catch { /* The failing implementation may already exit. */ }
  }
});

test("invalid handshake cleans up its backend before Electron exits", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"), 50, true, 300, false);
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: { ...process.env, HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: runtimeDir },
  });

  const startedAt = Date.now();
  await app.waitForEvent("close");
  expect(Date.now() - startedAt).toBeGreaterThanOrEqual(250);
  await expect.poll(async () => eventsText(events)).toContain("exited");
});

test("parent-control EOF terminates the backend fixture", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"));
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: {
      ...process.env,
      HERMES_PYTHON: executable,
      HERMES_RUNTIME_DIR: runtimeDir,
      HERMES_TEST_CLOSE_BACKEND_STDIN_AFTER_BOOTSTRAP: "1",
    },
  });

  await app.waitForEvent("close", { timeout: 2_000 });
  await expect.poll(async () => eventsText(events)).toContain("stdin-eof");
});

test("window creation failure stops the started backend before Electron exits", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"), 50, false, 300, false);
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: {
      ...process.env,
      HERMES_PYTHON: executable,
      HERMES_RUNTIME_DIR: runtimeDir,
      HERMES_TEST_FAIL_CREATE_WINDOW: "1",
    },
  });

  const startedAt = Date.now();
  await app.waitForEvent("close");
  expect(Date.now() - startedAt).toBeGreaterThanOrEqual(250);
  await expect.poll(async () => eventsText(events)).toContain("exited");
});

test("unexpected backend exit completes before Electron closes", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"), 50, false, 0, false, 100);
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: { ...process.env, HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: runtimeDir },
  });

  await app.waitForEvent("close");
  await expect.poll(async () => eventsText(events)).toContain("exited");
});

test("quit during pending startup stops its child before Electron exits", async ({}, testInfo) => {
  const executable = await writeLivenessBackend(testInfo.outputPath("fixture"), 1_500, false, 300, false, 500);
  const runtimeDir = testInfo.outputPath("runtime");
  const events = path.join(runtimeDir, "backend-events.log");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: { ...process.env, HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: runtimeDir },
  });

  await expect.poll(async () => eventsText(events)).toContain("started");
  const startedAt = Date.now();
  await app.evaluate(({ app: electronApp }) => electronApp.quit());
  await app.waitForEvent("close");
  expect(Date.now() - startedAt).toBeGreaterThanOrEqual(250);
  await expect.poll(async () => eventsText(events)).toContain("exited");
});

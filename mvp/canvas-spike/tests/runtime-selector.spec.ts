import { _electron as electron, expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import path from "node:path";

type FixtureMode = "ready" | "unavailable" | "blocked";

async function createOwnedBackendFixture(root: string, mode: FixtureMode, rejectExplicit = false, terminalEvents = false) {
  await mkdir(root, { recursive: true });
  const executable = path.join(root, "workbench-fixture.mjs");
  const source = `#!/usr/bin/env node
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
const args = process.argv.slice(2);
const runtimeArg = args.indexOf("--runtime-dir");
const runtimeDir = runtimeArg >= 0 ? args[runtimeArg + 1] : process.cwd();
fs.mkdirSync(runtimeDir, { recursive: true });
const logPath = path.join(runtimeDir, "requests.jsonl");
const mode = ${JSON.stringify(mode)};
const rejectExplicit = ${JSON.stringify(rejectExplicit)};
const terminalEvents = ${JSON.stringify(terminalEvents)};
let messageAccepted = false;
let runtimeRejected = false;
let bootstrap = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  bootstrap += chunk;
  const newline = bootstrap.indexOf("\\n");
  if (newline < 0 || globalThis.__started) return;
  globalThis.__started = true;
  const identity = JSON.parse(bootstrap.slice(0, newline));
  const server = http.createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
      fs.appendFileSync(logPath, JSON.stringify({ method: request.method, path: pathname, body: body ? JSON.parse(body) : null, lastEventId: request.headers["last-event-id"] ?? null }) + "\\n");
      const send = (status, value, contentType = "application/json") => {
        response.writeHead(status, { "content-type": contentType });
        response.end(typeof value === "string" ? value : JSON.stringify(value));
      };
      if (pathname === "/api/health") return send(200, { status: "ok", service: "hermes-workbench", instance_id: identity.instance_id, port: server.address().port });
      if (pathname === "/api/vault/lock") return send(200, { status: "locked" });
      if (pathname === "/api/vault/status") return send(200, { status: "locked", development_trust: process.env.WORKBENCH_FEDERATED_RUNTIME_DEVELOPMENT_TRUST ?? null });
      if (pathname === "/api/engine-host/status") return send(200, { enabled: true, state: "ready", protocol: "2.0", capabilities: { model: true, tools: true, skills: true, workspace: true, agui: true, max_frame_bytes: 1048576 }, runner_mode: "engine_host" });
      if (pathname === "/api/v1/engine-host") {
        const effectiveMode = runtimeRejected ? "blocked" : mode;
        return send(200, { v2: { enabled: true, protocol: "2.0", runtimes: [{ runtime_id: "python-term", build_id: "python-term-dev", state: effectiveMode === "ready" ? "ready" : "unavailable", capabilities: ["workspace.read"], selector: "python-term", selectable_for_new_commands: effectiveMode === "ready", admission_state: effectiveMode, trust_status: effectiveMode === "unavailable" ? null : "DEV_UNTRUSTED", admission_reason: effectiveMode === "ready" ? null : effectiveMode === "blocked" ? "proof_revoked" : "proof_missing" }] } });
      }
      if (pathname === "/api/agents") return send(200, []);
      if (pathname === "/api/providers") return send(200, [{ id: "lmstudio", name: "LM Studio", protocol: "lmstudio", headers: {}, base_url: "http://127.0.0.1:1234", model_aliases: { default: "local-agent" }, enabled: true, credential_mode: "none", credential_status: "not_required", capabilities: [], thinking_enabled: false, reasoning_effort: "high" }]);
      if (pathname.includes("/runtime-admissions/")) return send(200, { session_id: "ui-session-0", command_id: "public-command", selector: "python-term", runtime_id: "python-term", build_id: "python-term-dev", state: "ready", trust_status: "DEV_UNTRUSTED", reason_category: null });
      if (pathname.endsWith("/messages")) {
        const parsed = body ? JSON.parse(body) : {};
        if (rejectExplicit && parsed.runtime === "python-term") { runtimeRejected = true; return send(503, { detail: "runtime unavailable" }); }
        messageAccepted = true;
        return send(200, { session_id: "ui-session-0", command_id: "public-command", status: "queued", cursor: "cursor-1" });
      }
      if (pathname.endsWith("/events")) {
        if (!terminalEvents || !messageAccepted) return send(200, "", "text/event-stream");
        const events = [
          ["cursor-2", { name: "turn_queued", eventId: "event-queued", sequence: 1, value: { command_id: "public-command" } }],
          ["cursor-3", { name: "conversation.status", eventId: "event-running", sequence: 2, value: { status: "running" } }],
          ["cursor-4", { name: "tool.started", eventId: "event-tool-started", sequence: 3, value: { public_result: "workspace.read" } }],
          ["cursor-5", { name: "tool.completed", eventId: "event-tool-completed", sequence: 4, value: { public_result: "README" } }],
          ["cursor-6", { name: "turn_finished", eventId: "event-finished", sequence: 5, value: { status: "completed" } }],
        ];
        return send(200, events.map(([cursor, event]) => "id: " + cursor + "\\ndata: " + JSON.stringify(event) + "\\n\\n").join(""), "text/event-stream");
      }
      if (pathname === "/api/sessions" || (pathname.startsWith("/api/sessions/") && pathname.split("/").length === 4)) return send(200, { session_id: pathname.split("/").at(-1) });
      return send(404, { detail: "not found" });
    });
  });
  server.listen(0, "127.0.0.1", () => console.log(JSON.stringify({ service: "hermes-workbench", instance_id: identity.instance_id, port: server.address().port })));
});
`;
  await writeFile(executable, source, "utf8");
  await chmod(executable, 0o755);
  return executable;
}

async function requests(runtimeDir: string) {
  const raw = await readFile(path.join(runtimeDir, "requests.jsonl"), "utf8");
  return raw.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line) as { method: string; path: string; body: Record<string, unknown> | null; lastEventId: string | null });
}

async function launchFixture(testRoot: string, mode: FixtureMode, rejectExplicit = false, terminalEvents = false) {
  const runtimeDir = path.join(testRoot, "runtime");
  const executable = await createOwnedBackendFixture(testRoot, mode, rejectExplicit, terminalEvents);
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: {
      ...process.env,
      HERMES_PYTHON: executable,
      HERMES_RUNTIME_DIR: runtimeDir,
      WORKBENCH_PYTHON_TERM_DEVELOPMENT_TRUST: "true",
      WORKBENCH_FEDERATED_RUNTIME_DEVELOPMENT_TRUST: "true",
    },
  });
  return { app, runtimeDir };
}

async function createDeterministicModelProvider() {
  const requests: Array<Record<string, unknown>> = [];
  const server: Server = createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
      response.setHeader("content-type", "application/json");
      if (pathname === "/v1/models") {
        response.end(JSON.stringify({ data: [{ id: "fixture-model" }] }));
        return;
      }
      if (pathname !== "/v1/chat/completions") {
        response.statusCode = 404;
        response.end(JSON.stringify({ error: "not found" }));
        return;
      }
      const payload = JSON.parse(body) as Record<string, unknown>;
      requests.push(payload);
      const messages = Array.isArray(payload.messages) ? payload.messages as Array<{ role?: string }> : [];
      const tools = Array.isArray(payload.tools) ? payload.tools as Array<{ function?: { name?: string } }> : [];
      const toolName = tools[0]?.function?.name;
      const message = messages.some((item) => item.role === "tool")
        ? { role: "assistant", content: "Workspace smoke completed" }
        : {
            role: "assistant",
            content: null,
            tool_calls: [{
              id: "fixture-tool-call",
              type: "function",
              function: { name: toolName, arguments: JSON.stringify({ path: "/workspace/README.md" }) },
            }],
          };
      response.end(JSON.stringify({ choices: [{ index: 0, message, finish_reason: "stop" }], usage: { prompt_tokens: 5, completion_tokens: 5 } }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("model fixture failed to bind");
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

function prepareRealRuntime(runtimeDir: string): void {
  execFileSync(path.resolve("../.venv/bin/python"), [path.resolve("../scripts/prepare_python_term_dev_environment.py"), runtimeDir], {
    cwd: path.resolve(".."),
    env: { ...process.env, PYTHONPATH: path.resolve("../src") },
    encoding: "utf8",
  });
}

test("default Runtime preserves the legacy payload and explicit Python Term sends its selector", async ({}, testInfo) => {
  const { app, runtimeDir } = await launchFixture(testInfo.outputPath("owned-backend"), "ready");
  try {
    const page = await app.firstWindow();
    const runtime = page.getByRole("combobox", { name: "当前运行模式" });
    await expect(runtime).toHaveValue("");
    await page.getByRole("textbox", { name: "会话消息" }).fill("默认路径");
    await page.getByRole("button", { name: "发送" }).click();
    await expect.poll(async () => (await requests(runtimeDir)).filter((item) => item.path.endsWith("/messages")).length).toBe(1);
    const first = (await requests(runtimeDir)).find((item) => item.path.endsWith("/messages"));
    expect(first?.body).not.toHaveProperty("runtime");

    await page.reload();
    await runtime.selectOption("python-term");
    await page.getByRole("textbox", { name: "会话消息" }).fill("显式 Python Term");
    await page.getByRole("button", { name: "发送" }).click();
    await expect.poll(async () => (await requests(runtimeDir)).filter((item) => item.path.endsWith("/messages")).length).toBe(2);
    const all = (await requests(runtimeDir)).filter((item) => item.path.endsWith("/messages"));
    expect(all[1]?.body).toMatchObject({ runtime: "python-term" });
    await expect(runtime).toBeDisabled();
  } finally {
    await app.close();
  }
});

test("unavailable Runtime exposes its stable reason and cannot be selected", async ({}, testInfo) => {
  const { app } = await launchFixture(testInfo.outputPath("owned-backend"), "unavailable");
  try {
    const page = await app.firstWindow();
    const runtime = page.getByRole("combobox", { name: "当前运行模式" });
    await expect(runtime.getByRole("option", { name: /proof_missing/ })).toBeDisabled();
    await expect(runtime).toHaveValue("");
  } finally {
    await app.close();
  }
});

test("Engine Host exposes admission trust and Electron only permits the read-only command diagnostic", async ({}, testInfo) => {
  const { app } = await launchFixture(testInfo.outputPath("owned-backend"), "blocked");
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "Agent 配置" }).click();
    await expect(page.getByTestId("runtime-diagnostic-python-term")).toContainText("blocked · DEV_UNTRUSTED · proof_revoked");
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/sessions/ui-session-0/runtime-admissions/public-command" }))).resolves.toMatchObject({ status: 200 });
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/vault/status" }))).resolves.toMatchObject({ body: { development_trust: "true" } });
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "POST", path: "/sessions/ui-session-0/runtime-admissions/public-command" }))).rejects.toThrow("invalid local API request");
  } finally {
    await app.close();
  }
});

test("explicit Runtime failure is stable and never falls back to the default path", async ({}, testInfo) => {
  const { app, runtimeDir } = await launchFixture(testInfo.outputPath("owned-backend"), "ready", true);
  try {
    const page = await app.firstWindow();
    await page.getByRole("combobox", { name: "当前运行模式" }).selectOption("python-term");
    await page.getByRole("textbox", { name: "会话消息" }).fill("只执行一次");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByTestId("conversation-status")).toContainText("runtime_unavailable");
    await expect(page.getByLabel("当前运行模式").locator('option[value="python-term"]')).toContainText("proof_revoked");
    await expect.poll(async () => (await requests(runtimeDir)).filter((item) => item.path.endsWith("/messages")).length).toBe(1);
    const message = (await requests(runtimeDir)).find((item) => item.path.endsWith("/messages"));
    expect(message?.body).toMatchObject({ runtime: "python-term" });
  } finally {
    await app.close();
  }
});

test("restores the durable SSE cursor after refresh and renders tool to terminal progress", async ({}, testInfo) => {
  const { app, runtimeDir } = await launchFixture(testInfo.outputPath("owned-backend"), "ready", false, true);
  try {
    const page = await app.firstWindow();
    await page.getByRole("combobox", { name: "当前运行模式" }).selectOption("python-term");
    await page.getByRole("textbox", { name: "会话消息" }).fill("读取 /workspace/README.md");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByTestId("conversation-status")).toContainText("已完成");
    await expect(page.getByText("工具执行 · Tool evidence").last()).toBeVisible();

    await writeFile(path.join(runtimeDir, "requests.jsonl"), "", "utf8");
    await page.reload();
    await expect.poll(async () => {
      try { return (await requests(runtimeDir)).filter((item) => item.path.endsWith("/events")).length; } catch { return 0; }
    }).toBeGreaterThan(0);
    const firstEventsRequest = (await requests(runtimeDir)).find((item) => item.path.endsWith("/events"));
    expect(firstEventsRequest?.lastEventId).toBeNull();
  } finally {
    await app.close();
  }
});

test("real prepared Python Term environment supports the Electron-owned acceptance path", async ({}, testInfo) => {
  const runtimeDir = testInfo.outputPath("prepared-runtime");
  const provider = await createDeterministicModelProvider();
  prepareRealRuntime(runtimeDir);
  const baseEnvironment = {
    ...process.env,
    HERMES_PYTHON: path.resolve("../.venv/bin/python"),
    HERMES_RUNTIME_DIR: runtimeDir,
    HERMES_LMSTUDIO_BASE_URL: provider.baseUrl,
  };
  const bootstrap = await electron.launch({ args: [path.resolve(".")], env: baseEnvironment });
  try {
    const bootstrapPage = await bootstrap.firstWindow();
    const configured = await bootstrapPage.evaluate((baseUrl) => (window as any).workbenchBridge.apiRequest({
      method: "POST",
      path: "/providers",
      body: {
        id: "fixture-provider",
        name: "Deterministic LM Studio",
        protocol: "lmstudio",
        credential_mode: "none",
        base_url: baseUrl,
        model_aliases: { default: "fixture-model", "local-agent": "fixture-model" },
        capabilities: ["tool_calling"],
        enabled: true,
        thinking_enabled: false,
        reasoning_effort: "high",
      },
    }), provider.baseUrl);
    expect(configured).toMatchObject({ status: 201 });
  } finally {
    await bootstrap.close();
  }

  const app = await electron.launch({
    args: [path.resolve(".")],
    env: {
      ...baseEnvironment,
      WORKBENCH_ENGINE_HOST_V2_ENABLED: "true",
      WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED: "true",
      WORKBENCH_PYTHON_TERM_DEVELOPMENT_TRUST: "true",
    },
  });
  try {
    const page = await app.firstWindow();
    const runtime = page.getByRole("combobox", { name: "当前运行模式" });
    await expect(runtime.getByRole("option", { name: /Codex Harness） · DEV_UNTRUSTED/ })).toBeEnabled();
    await runtime.selectOption("python-term");
    await page.getByRole("textbox", { name: "会话消息" }).fill("读取 /workspace/README.md");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByTestId("conversation-status")).toContainText("已完成", { timeout: 15_000 });
    const eventsResponse = await page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/sessions/ui-session-0/events" }));
    const publicEvents = String(eventsResponse.text).split("\n").filter((line) => line.startsWith("data: ")).map((line) => JSON.parse(line.slice(6)));
    const started = publicEvents.findIndex((event) => event.type === "TOOL_CALL_START");
    const completed = publicEvents.findIndex((event) => event.type === "TOOL_CALL_END");
    const finished = publicEvents.findIndex((event) => event.name === "runtime.status.changed" && event.value?.status === "completed");
    expect(started).toBeGreaterThanOrEqual(0);
    expect(completed).toBeGreaterThan(started);
    expect(finished).toBeGreaterThan(completed);
    const queued = publicEvents.find((event) => event.name === "turn_queued");
    expect(queued?.value?.command_id).toBeTruthy();
    const admission = await page.evaluate(({ commandId }) => (window as any).workbenchBridge.apiRequest({ method: "GET", path: `/sessions/ui-session-0/runtime-admissions/${commandId}` }), { commandId: queued.value.command_id });
    expect(admission).toMatchObject({ status: 200, body: { selector: "python-term", state: "ready", trust_status: "DEV_UNTRUSTED" } });
    expect(provider.requests).toHaveLength(2);
    expect(provider.requests[0].tools).toEqual([expect.objectContaining({ type: "function" })]);
    const secondMessages = provider.requests[1].messages as Array<{ role?: string; content?: string }>;
    expect(secondMessages.find((message) => message.role === "tool")?.content).toContain("Python Term DEV Smoke Workspace");
  } finally {
    await app.close();
    await provider.close();
  }
});

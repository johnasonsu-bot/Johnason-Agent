import { app, BrowserWindow, ipcMain, type IpcMainInvokeEvent } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import path from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const SERVICE_IDENTITY = "hermes-workbench";
const CAPABILITY_HEADER = "X-Workbench-Capability";
const allowedApiRequests = new Set([
  "GET /api/vault/status", "POST /api/vault/create", "POST /api/vault/unlock", "POST /api/vault/lock", "POST /api/vault/recover",
  "GET /api/providers", "POST /api/providers",
  "GET /api/engine-host/status",
]);

interface ApiRequest { method: "GET" | "POST" | "PUT" | "DELETE"; path: string; body?: Record<string, unknown>; headers?: Record<string, string>; }
interface BackendHandshake { service: string; instance_id: string; port: number; }
interface BackendProcess {
  child: ChildProcessWithoutNullStreams;
  apiBase: string;
  capability: string;
  instanceId: string;
}

let mainWindow: BrowserWindow | null = null;
let trustedDocumentUrl = "";
let backend: BackendProcess | null = null;
let startingBackend: Promise<void> | null = null;
let startingChild: ChildProcessWithoutNullStreams | null = null;
let stoppingBackend: Promise<void> | null = null;
let stoppingAndExiting: Promise<void> | null = null;
let quitAfterCleanup = false;
let quitting = false;

function isApiRequest(value: unknown): value is ApiRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<ApiRequest>;
  if ((request.method !== "GET" && request.method !== "POST" && request.method !== "PUT" && request.method !== "DELETE") || typeof request.path !== "string") return false;
  if (!allowedApiRequests.has(`${request.method} /api${request.path}`)) {
    const providerPath = /^\/providers\/[A-Za-z0-9_-]{1,64}(?:\/(secret|test|models))?$/.exec(request.path);
    const conversationPath = /^\/sessions(?:\/[A-Za-z0-9_-]{1,64}(?:\/(messages|events|interventions|pause|resume))?)?$/.exec(request.path);
    const orchestrationResumePath = /^\/sessions\/[A-Za-z0-9_-]{1,64}\/orchestrations\/[A-Za-z0-9_-]{1,128}\/resume$/.exec(request.path);
    const agentPath = /^\/agents(?:\/[A-Za-z0-9_-]{1,64})?$/.exec(request.path);
    const artifactPath = /^\/artifacts\/sha256%3A[a-f0-9]{64}$/i.exec(request.path);
    const graphPlanPath = /^\/sessions\/[A-Za-z0-9_-]{1,64}\/plans(?:\/[A-Za-z0-9._:-]{1,128}\/versions\/\d+(?:\/(approve|replan))?)?$/.exec(request.path);
    const graphInterruptPath = /^\/graph-runs\/[A-Za-z0-9._:-]{1,128}\/interrupts\/[A-Za-z0-9._:-]{1,128}$/.exec(request.path);
    if (!providerPath && !conversationPath && !orchestrationResumePath && !agentPath && !artifactPath && !graphPlanPath && !graphInterruptPath) return false;
    if (graphInterruptPath) return request.method === "POST";
    if (graphPlanPath) {
      const operation = graphPlanPath[1];
      return (!operation && request.method === "GET")
        || (operation === "approve" && request.method === "POST")
        || (operation === "replan" && request.method === "POST")
        || (/\/plans$/.test(request.path) && request.method === "POST");
    }
    if (artifactPath) return request.method === "GET";
    if (agentPath) {
      return (request.path === "/agents" && ["GET", "POST"].includes(request.method))
        || (request.path !== "/agents" && request.method === "PUT");
    }
    if (orchestrationResumePath) return request.method === "POST";
    if (conversationPath) {
      const operation = conversationPath[1];
      return (operation === "events" && request.method === "GET")
        || (operation === "messages" && request.method === "POST")
        || (operation === "interventions" && request.method === "POST")
        || ((operation === "pause" || operation === "resume") && request.method === "POST")
        || (!operation && request.method === "POST");
    }
    if (!providerPath) return false;
    const operation = providerPath[1];
    if (!((!operation && request.method === "DELETE") || (operation === "secret" && request.method === "POST") || (operation === "test" && request.method === "POST") || (operation === "models" && request.method === "GET"))) return false;
  }
  if (request.body !== undefined && (typeof request.body !== "object" || request.body === null || Array.isArray(request.body) || JSON.stringify(request.body).length > 16_384)) return false;
  if (request.headers !== undefined && (typeof request.headers !== "object" || request.headers === null || Array.isArray(request.headers))) return false;
  return true;
}

function sameTrustedDocument(value: string): boolean {
  try {
    const candidate = new URL(value);
    candidate.hash = "";
    candidate.search = "";
    return candidate.toString() === trustedDocumentUrl;
  } catch {
    return false;
  }
}

function assertTrustedSender(event: IpcMainInvokeEvent): void {
  const frame = event.senderFrame;
  if (
    mainWindow === null
    || mainWindow.isDestroyed()
    || event.sender !== mainWindow.webContents
    || frame === null
    || frame !== mainWindow.webContents.mainFrame
    || !sameTrustedDocument(frame.url)
  ) {
    throw new Error("untrusted IPC sender");
  }
}

function childEnvironment(): NodeJS.ProcessEnv {
  const safe: NodeJS.ProcessEnv = { PYTHONUNBUFFERED: "1" };
  for (const name of [
    "PATH", "SystemRoot", "WINDIR", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONPATH",
    "WORKBENCH_ENGINE_HOST_ENABLED",
    "WORKBENCH_ENGINE_HOST_COMMAND_JSON",
    "WORKBENCH_ENGINE_HOST_PROVIDER_ALLOWLIST_JSON",
  ]) {
    if (process.env[name] !== undefined) safe[name] = process.env[name];
  }
  return safe;
}

function pythonExecutable(): string {
  const configured = process.env.HERMES_PYTHON;
  const bundled = process.platform === "win32"
    ? "../../.venv/Scripts/python.exe"
    : "../../.venv/bin/python";
  const executable = configured ?? path.resolve(__dirname, bundled);
  if (!path.isAbsolute(executable)) throw new Error("Hermes Python executable must be absolute");
  return executable;
}

function runtimeDirectory(): string {
  return path.resolve(process.env.HERMES_RUNTIME_DIR ?? path.join(app.getPath("userData"), "workbench-runtime"));
}

function lmStudioBaseUrl(): string {
  const value = process.env.HERMES_LMSTUDIO_BASE_URL ?? "http://127.0.0.1:1234";
  const parsed = new URL(value);
  if (parsed.protocol !== "http:" || !["127.0.0.1", "::1", "localhost"].includes(parsed.hostname)) {
    throw new Error("LM Studio bootstrap URL must use loopback HTTP");
  }
  return parsed.origin;
}

function readHandshake(child: ChildProcessWithoutNullStreams, instanceId: string): Promise<BackendHandshake> {
  return new Promise((resolve, reject) => {
    const lines = createInterface({ input: child.stdout });
    const timer = setTimeout(() => finish(new Error("Workbench backend handshake timed out")), 15_000);
    const onExit = () => finish(new Error("Workbench backend exited before handshake"));
    let settled = false;
    const finish = (error?: Error, value?: BackendHandshake) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.off("exit", onExit);
      lines.close();
      if (error) reject(error); else resolve(value!);
    };
    child.once("exit", onExit);
    lines.once("line", (line) => {
      try {
        const value = JSON.parse(line) as Partial<BackendHandshake>;
        if (
          value.service !== SERVICE_IDENTITY
          || value.instance_id !== instanceId
          || !Number.isInteger(value.port)
          || (value.port ?? 0) < 1
          || (value.port ?? 0) > 65_535
        ) throw new Error("invalid Workbench backend handshake");
        finish(undefined, value as BackendHandshake);
      } catch {
        finish(new Error("invalid Workbench backend handshake"));
      }
    });
  });
}

async function authenticatedBackendRequest(
  owned: BackendProcess,
  pathname: string,
  init: RequestInit = {},
  timeoutMs = 5_000,
): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set(CAPABILITY_HEADER, owned.capability);
  return fetch(`${owned.apiBase}${pathname}`, {
    ...init,
    headers,
    redirect: "error",
    signal: AbortSignal.timeout(timeoutMs),
  });
}

function apiRequestTimeout(pathname: string, method: string): number {
  // Conversation POSTs only enqueue a durable turn. Keep this control-plane
  // request short; model/provider work continues in the Python Worker and is
  // consumed through the cursor-based events endpoint.
  if (method === "POST" && /^\/api\/sessions\/[^/]+\/messages$/.test(pathname)) return 15_000;
  if (method === "GET" && /^\/api\/sessions\/[^/]+\/events$/.test(pathname)) return 30_000;
  return 5_000;
}

async function verifyBackendIdentity(owned: BackendProcess): Promise<void> {
  const response = await authenticatedBackendRequest(owned, "/api/health");
  if (!response.ok || response.redirected) throw new Error("Workbench backend health check failed");
  const body = await response.json() as Partial<BackendHandshake> & { status?: string };
  if (body.status !== "ok" || body.service !== SERVICE_IDENTITY || body.instance_id !== owned.instanceId) {
    throw new Error("Workbench backend identity mismatch");
  }
}

async function startBackend(): Promise<void> {
  if (backend !== null) return;
  if (startingBackend !== null) return startingBackend;

  const pending = (async () => {
    if (stoppingBackend !== null) await stoppingBackend;
    if (quitting) throw new Error("Workbench backend startup was cancelled");
    const capability = randomBytes(32).toString("base64url");
    const instanceId = randomUUID();
    const child = spawn(
      pythonExecutable(),
      [
        "-m", "workbench.main", "--electron-owned",
        "--runtime-dir", runtimeDirectory(),
        "--host", "127.0.0.1",
        "--port", "0",
        "--lmstudio-base-url", lmStudioBaseUrl(),
      ],
      { env: childEnvironment(), stdio: ["pipe", "pipe", "pipe"] },
    );
    startingChild = child;
    child.stderr.resume();
    child.stdin.write(`${JSON.stringify({ capability, instance_id: instanceId })}\n`);
    if (process.env.HERMES_TEST_CLOSE_BACKEND_STDIN_AFTER_BOOTSTRAP === "1") child.stdin.end();
    try {
      const handshake = await readHandshake(child, instanceId);
      child.stdout.resume();
      const owned = {
        child,
        apiBase: `http://127.0.0.1:${handshake.port}`,
        capability,
        instanceId,
      };
      await verifyBackendIdentity(owned);
      if (quitting || startingChild !== child) throw new Error("Workbench backend startup was cancelled");
      backend = owned;
      startingChild = null;
      child.once("exit", () => {
        if (backend?.child === child) {
          backend = null;
          if (!quitting) void stopAndExit(1);
        }
      });
    } catch (error) {
      if (startingChild === child) startingChild = null;
      if (!child.stdin.destroyed) child.stdin.end();
      if (child.exitCode === null && child.signalCode === null) child.kill();
      if (!await waitForExit(child, 3_000)) {
        child.kill("SIGKILL");
        await waitForExit(child, 2_000);
      }
      throw error;
    }
  })();
  startingBackend = pending;
  try {
    await pending;
  } finally {
    if (startingBackend === pending) startingBackend = null;
  }
}

function waitForExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    child.once("exit", () => { clearTimeout(timer); resolve(true); });
  });
}

async function stopBackend(): Promise<void> {
  if (stoppingBackend !== null) return stoppingBackend;
  const owned = backend;
  const pending = startingChild;
  backend = null;
  startingChild = null;
  if (owned === null && pending === null) return;
  stoppingBackend = (async () => {
    if (owned !== null) {
      try {
        await authenticatedBackendRequest(owned, "/api/vault/lock", { method: "POST" }, 1_000);
      } catch {
        // The process lifespan also locks the vault; termination remains mandatory.
      }
    }
    for (const child of new Set([owned?.child, pending])) {
      if (child === null || child === undefined) continue;
      if (!child.stdin.destroyed) child.stdin.end();
      if (child.exitCode === null && child.signalCode === null) child.kill();
      if (!await waitForExit(child, 3_000)) {
        child.kill("SIGKILL");
        await waitForExit(child, 2_000);
      }
    }
  })().finally(() => { stoppingBackend = null; });
  return stoppingBackend;
}

async function stopAndExit(code: number): Promise<void> {
  if (stoppingAndExiting !== null) return stoppingAndExiting;
  stoppingAndExiting = (async () => {
    await stopBackend();
    quitting = true;
    quitAfterCleanup = true;
    process.exitCode = code;
    app.quit();
  })();
  return stoppingAndExiting;
}

ipcMain.handle("api.request", async (event, request: unknown) => {
  assertTrustedSender(event);
  if (!isApiRequest(request)) throw new Error("invalid local API request");
  const owned = backend;
  if (owned === null) throw new Error("local Workbench backend is unavailable");
  const response = await authenticatedBackendRequest(owned, `/api${request.path}`, {
    method: request.method,
    headers: { ...(request.body === undefined ? {} : { "content-type": "application/json" }), ...(request.headers ?? {}) },
    body: request.body === undefined ? undefined : JSON.stringify(request.body),
  }, apiRequestTimeout(`/api${request.path}`, request.method));
  const responseText = await response.text();
  if (responseText.length > 1_048_576) throw new Error("local API response is too large");
  let body: unknown = null;
  try { body = JSON.parse(responseText); } catch { /* Endpoint responses are JSON by contract. */ }
  return { status: response.status, body, text: responseText };
});

interface InterventionCommand {
  runId: string;
  artifactId: string;
  kind: "annotation";
  payload: Record<string, unknown>;
}

function isInterventionCommand(value: unknown): value is InterventionCommand {
  if (!value || typeof value !== "object") return false;
  const command = value as Partial<InterventionCommand>;
  return typeof command.runId === "string"
    && typeof command.artifactId === "string"
    && command.kind === "annotation"
    && Boolean(command.payload)
    && typeof command.payload === "object";
}

ipcMain.handle("intervention.submit", (event, command: unknown) => {
  assertTrustedSender(event);
  if (!isInterventionCommand(command)) throw new Error("invalid intervention command");
  return { accepted: true, runId: command.runId, artifactId: command.artifactId };
});

function createWindow(): BrowserWindow {
  if (mainWindow !== null && !mainWindow.isDestroyed()) return mainWindow;
  if (process.env.HERMES_TEST_FAIL_CREATE_WINDOW === "1") {
    throw new Error("Workbench window creation failed by test request");
  }
  const rendererPath = path.join(__dirname, "../dist/index.html");
  trustedDocumentUrl = pathToFileURL(rendererPath).toString();
  const window = new BrowserWindow({
    width: 1200,
    height: 800,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
    },
  });
  mainWindow = window;
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  window.webContents.on("will-navigate", (event, target) => {
    if (!sameTrustedDocument(target)) event.preventDefault();
  });
  window.webContents.on("will-redirect", (event, target) => {
    if (!sameTrustedDocument(target)) event.preventDefault();
  });
  window.webContents.on("render-process-gone", () => {
    if (!quitting) void stopAndExit(1);
  });
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
    void stopBackend();
  });
  void window.loadFile(rendererPath);
  return window;
}

async function ready(): Promise<void> {
  try {
    await startBackend();
    createWindow();
  } catch {
    await stopAndExit(1);
  }
}

void app.whenReady().then(ready).catch(() => { void stopAndExit(1); });

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    void (async () => {
      try {
        await startBackend();
        createWindow();
      } catch {
        await stopAndExit(1);
      }
    })();
  }
});

app.on("before-quit", (event) => {
  quitting = true;
  if (quitAfterCleanup || (backend === null && startingChild === null && startingBackend === null && stoppingBackend === null)) return;
  event.preventDefault();
  void stopBackend().finally(() => {
    quitAfterCleanup = true;
    app.quit();
  });
});

app.on("window-all-closed", () => app.quit());

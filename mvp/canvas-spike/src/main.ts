import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";

const configuredApiBase = new URL(process.env.HERMES_API_BASE ?? "http://127.0.0.1:8765");
if (configuredApiBase.protocol !== "http:" || !["127.0.0.1", "::1"].includes(configuredApiBase.hostname)) {
  throw new Error("Hermes API must use a loopback HTTP origin");
}
const apiBase = configuredApiBase.origin;
const allowedApiRequests = new Set([
  "GET /api/vault/status", "POST /api/vault/create", "POST /api/vault/unlock", "POST /api/vault/lock",
  "GET /api/providers", "POST /api/providers",
]);

interface ApiRequest { method: "GET" | "POST" | "DELETE"; path: string; body?: Record<string, unknown>; }

function isApiRequest(value: unknown): value is ApiRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Partial<ApiRequest>;
  if ((request.method !== "GET" && request.method !== "POST" && request.method !== "DELETE") || typeof request.path !== "string") return false;
  if (!allowedApiRequests.has(`${request.method} /api${request.path}`)) {
    const providerPath = /^\/providers\/[A-Za-z0-9_-]+(?:\/(secret|test|models))?$/.exec(request.path);
    if (!providerPath) return false;
    const operation = providerPath[1];
    if (!((!operation && request.method === "DELETE") || (operation === "secret" && request.method === "POST") || (operation === "test" && request.method === "POST") || (operation === "models" && request.method === "GET"))) return false;
  }
  if (request.body !== undefined && (typeof request.body !== "object" || request.body === null || Array.isArray(request.body) || JSON.stringify(request.body).length > 16_384)) return false;
  return true;
}

ipcMain.handle("api.request", async (_event, request: unknown) => {
  if (!isApiRequest(request)) throw new Error("invalid local API request");
  const response = await fetch(`${apiBase}/api${request.path}`, {
    method: request.method,
    redirect: "error",
    headers: request.body === undefined ? undefined : { "content-type": "application/json" },
    body: request.body === undefined ? undefined : JSON.stringify(request.body),
  });
  let body: unknown = null;
  try { body = await response.json(); } catch { /* Endpoint responses are JSON by contract. */ }
  return { status: response.status, body };
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

ipcMain.handle("intervention.submit", (_event, command: unknown) => {
  if (!isInterventionCommand(command)) throw new Error("invalid intervention command");
  return { accepted: true, runId: command.runId, artifactId: command.artifactId };
});

function createWindow(): BrowserWindow {
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

  void window.loadFile(path.join(__dirname, "../dist/index.html"));
  return window;
}

void app.whenReady().then(() => {
  createWindow();
});

app.on("window-all-closed", () => app.quit());

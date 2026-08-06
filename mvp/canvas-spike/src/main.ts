import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";

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

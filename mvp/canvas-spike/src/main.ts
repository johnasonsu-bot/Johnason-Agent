import { app, BrowserWindow } from "electron";
import path from "node:path";

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


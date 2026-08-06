import { _electron as electron, expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import path from "node:path";

test("packages platform-native Python defaults", async () => {
  const mainBundle = await readFile(path.resolve("dist-electron/main.js"), "utf8");
  expect(mainBundle).toContain(".venv/bin/python");
  expect(mainBundle).toContain(".venv/Scripts/python.exe");
});

async function squatter() {
  const requests: string[] = [];
  const server: Server = createServer((request, response) => {
    requests.push(new URL(request.url ?? "/", "http://127.0.0.1").pathname);
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ status: "locked" }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("squatter did not bind");
  return {
    base: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

function ownedEnvironment(runtimeDir: string, fakeBase: string) {
  return {
    ...process.env,
    HERMES_PYTHON: path.resolve("../.venv/bin/python"),
    HERMES_RUNTIME_DIR: runtimeDir,
    HERMES_API_BASE: fakeBase,
  };
}

test("owns a random-port backend and ignores a fixed-port squatter", async ({}, testInfo) => {
  const fake = await squatter();
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(testInfo.outputPath("runtime"), fake.base),
  });
  try {
    const page = await app.firstWindow();
    await page.getByRole("link", { name: "模型供应商" }).click();
    await expect(page.getByRole("button", { name: "创建并解锁" })).toBeVisible();
    expect(fake.requests).toEqual([]);
    const bridgeKeys = await page.evaluate(() => Object.keys((window as any).workbenchBridge));
    expect(bridgeKeys).not.toContain("capability");
    expect(bridgeKeys).not.toContain("apiBase");
  } finally {
    await app.close();
    await fake.close();
  }
});

test("rejects IPC from an untrusted frame even when it has the preload", async ({}, testInfo) => {
  const fake = await squatter();
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(testInfo.outputPath("runtime"), fake.base),
  });
  try {
    await app.firstWindow();
    const untrustedWindow = app.waitForEvent("window");
    const windowId = await app.evaluate(
      async ({ BrowserWindow }, preload) => {
        const candidate = new BrowserWindow({
          show: false,
          webPreferences: {
            preload,
            contextIsolation: true,
            sandbox: true,
            nodeIntegration: false,
          },
        });
        await candidate.loadURL("data:text/html,<title>untrusted</title>");
        return candidate.id;
      },
      path.resolve("dist-electron/preload.js"),
    );
    const page = await untrustedWindow;

    await expect(
      page.evaluate(() =>
        (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/vault/status" }),
      ),
    ).rejects.toThrow();
    expect(fake.requests).toEqual([]);
    await app.evaluate(({ BrowserWindow }, id) => BrowserWindow.fromId(id)?.close(), windowId);
  } finally {
    await app.close();
    await fake.close();
  }
});

test("blocks unexpected navigation and window creation and ships a strict CSP", async ({}, testInfo) => {
  const fake = await squatter();
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(testInfo.outputPath("runtime"), fake.base),
  });
  try {
    const page = await app.firstWindow();
    const csp = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("connect-src 'none'");

    const initialUrl = page.url();
    const initialWindows = app.windows().length;
    await page.evaluate(() => window.open("about:blank", "_blank"));
    await expect.poll(() => app.windows().length).toBe(initialWindows);
    await page.evaluate(() => { window.location.href = "data:text/html,untrusted"; });
    await expect.poll(() => page.url()).toBe(initialUrl);
  } finally {
    await app.close();
    await fake.close();
  }
});

test("renderer crash terminates the backend and releases the vault writer", async ({}, testInfo) => {
  const runtime = testInfo.outputPath("crash-runtime");
  const password = `runtime-${randomUUID()}`;
  const environment = ownedEnvironment(runtime, "http://127.0.0.1:1");
  const first = await electron.launch({ args: [path.resolve(".")], env: environment });
  const page = await first.firstWindow();
  await page.getByRole("link", { name: "模型供应商" }).click();
  await page.getByLabel("主密码").fill(password);
  await page.getByRole("button", { name: "创建并解锁" }).click();
  await expect(page.getByRole("button", { name: "锁定保险库" })).toBeVisible();

  const closed = first.waitForEvent("close");
  await first.evaluate(({ BrowserWindow }) => {
    BrowserWindow.getAllWindows()[0]?.webContents.forcefullyCrashRenderer();
  });
  await closed;

  const second = await electron.launch({ args: [path.resolve(".")], env: environment });
  try {
    const restarted = await second.firstWindow();
    await restarted.getByRole("link", { name: "模型供应商" }).click();
    await expect(restarted.getByRole("button", { name: "解锁" })).toBeVisible();
    await restarted.getByLabel("主密码").fill(password);
    await restarted.getByRole("button", { name: "解锁" }).click();
    await expect(restarted.getByRole("button", { name: "锁定保险库" })).toBeVisible();
  } finally {
    await second.close();
  }
});

import { _electron as electron, expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import { spawnSync } from "node:child_process";
import path from "node:path";

type UpstreamRequest = { method: string; path: string; body: Record<string, unknown> | null };

async function fakeLmStudio() {
  const requests: UpstreamRequest[] = [];
  const server: Server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null;
    const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;
    requests.push({ method: request.method ?? "GET", path: pathname, body });
    const send = (value: unknown, status = 200) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(value));
    };
    if (pathname === "/v1/models") {
      return send({ data: [{ id: "first-model" }, { id: "second-model" }] });
    }
    if (pathname === "/v1/chat/completions") {
      return send({ choices: [{ message: { content: "ready" } }] });
    }
    return send({ detail: "not found" }, 404);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("LM Studio fake did not bind");
  return {
    base: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

function ownedEnvironment(runtimeDir: string, lmStudioBase = "http://127.0.0.1:1234") {
  return {
    ...process.env,
    HERMES_PYTHON: path.resolve("../.venv/bin/python"),
    HERMES_RUNTIME_DIR: runtimeDir,
    HERMES_LMSTUDIO_BASE_URL: lmStudioBase,
  };
}

async function runtimeFiles(root: string): Promise<string[]> {
  const found: string[] = [];
  async function visit(current: string) {
    for (const name of await readdir(current)) {
      const candidate = path.join(current, name);
      if ((await stat(candidate)).isDirectory()) await visit(candidate);
      else found.push(candidate);
    }
  }
  await visit(root);
  return found;
}

test("uses a narrow IPC bridge for the Electron-owned backend", async ({}, testInfo) => {
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(testInfo.outputPath("runtime")),
  });
  try {
    const page = await app.firstWindow();
    expect(await page.evaluate(() => typeof (window as any).workbenchBridge.apiRequest)).toBe("function");
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "PUT", path: "/providers" }))).rejects.toThrow();
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/health" }))).rejects.toThrow();
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "DELETE", path: "/providers/not.valid" }))).rejects.toThrow();
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/v1/engine-host" }))).resolves.toMatchObject({ status: 200 });
    await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "POST", path: "/v1/engine-host" }))).rejects.toThrow();
  } finally {
    await app.close();
  }
});

test("real Workbench backend completes the Batch 1 Provider Center lifecycle", async ({}, testInfo) => {
  const upstream = await fakeLmStudio();
  const runtime = testInfo.outputPath("workbench-runtime");
  const password = `runtime-${randomUUID()}`;
  const credential = `runtime-${randomUUID()}`;
  const launch = () => electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(runtime, upstream.base),
  });

  let app = await launch();
  try {
    let page = await app.firstWindow();
    page.on("dialog", (dialog) => void dialog.accept());
    await page.getByRole("link", { name: "模型供应商" }).click();
    await page.getByLabel("主密码").fill(password);
    await page.getByRole("button", { name: "创建并解锁" }).click();
    await expect(page.getByLabel("主密码")).toHaveCount(0);

    await page.getByRole("button", { name: "使用 LM Studio" }).click();
    await page.getByLabel("基础地址").fill(upstream.base);
    await expect(page.getByLabel("启用供应商")).toBeChecked();
    await page.getByRole("button", { name: "保存供应商" }).click();
    await expect(page.getByText("无需凭据")).toBeVisible();
    await page.getByRole("button", { name: "发现模型" }).click();
    await page.getByRole("combobox", { name: "默认模型" }).selectOption("second-model");
    await expect(page.getByText("默认模型已更新")).toBeVisible();
    await page.getByRole("button", { name: "测试连接" }).click();
    await expect(page.getByText("连接正常")).toBeVisible();
    expect(upstream.requests.some((request) => request.path === "/v1/models")).toBeTruthy();
    await expect.poll(
      () => upstream.requests.some((request) => request.path === "/v1/chat/completions" && request.body?.model === "second-model"),
      { message: "LM Studio completion should use the persisted default model" },
    ).toBeTruthy();

    await page.getByLabel("启用供应商").uncheck();
    await page.getByRole("button", { name: "保存供应商" }).click();
    await expect(page.getByRole("button", { name: "测试连接" })).toBeDisabled();
    await page.getByLabel("启用供应商").check();
    await page.getByRole("button", { name: "保存供应商" }).click();

    await page.getByRole("button", { name: "使用 DeepSeek" }).click();
    await expect(page.getByRole("combobox", { name: "推理强度" })).toHaveValue("high");
    await page.getByRole("combobox", { name: "推理强度" }).selectOption("max");
    await page.getByLabel("API 密钥").fill(credential);
    await page.getByRole("button", { name: "保存供应商" }).click();
    await expect(page.getByLabel("API 密钥")).toHaveValue("");
    await expect(page.getByText("凭据已配置")).toBeVisible();
    await page.getByLabel("基础地址").fill("https://changed-provider.invalid");
    await page.getByRole("button", { name: "保存供应商" }).click();
    await expect(page.getByText("未配置凭据")).toBeVisible();
    await expect(page.getByRole("combobox", { name: "推理强度" })).toHaveValue("max");
    await page.getByRole("button", { name: "删除供应商" }).click();
    await expect(page.getByText("供应商已删除")).toBeVisible();

    expect(await page.evaluate(() => ({
      local: localStorage.length,
      session: sessionStorage.length,
      localKeys: Object.keys(localStorage),
      text: document.body.innerText,
    }))).toEqual(expect.objectContaining({ session: 0 }));
    expect(await page.evaluate(() => Object.keys(localStorage).some((key) => /provider|secret/i.test(key)))).toBeFalsy();
    expect(await page.evaluate((values) => !document.body.innerText.includes(values.password) && !document.body.innerText.includes(values.credential), { password, credential })).toBeTruthy();

    await page.getByRole("button", { name: "锁定保险库" }).click();
    await expect(page.getByRole("button", { name: "解锁" })).toBeVisible();
    await app.close();

    app = await launch();
    page = await app.firstWindow();
    await page.getByRole("link", { name: "模型供应商" }).click();
    await expect(page.getByRole("button", { name: "解锁" })).toBeVisible();
    await page.getByLabel("主密码").fill(password);
    await page.getByRole("button", { name: "解锁" }).click();
    await expect(page.getByRole("combobox", { name: "默认模型" })).toHaveValue("second-model");
    await expect(page.getByLabel("启用供应商")).toBeChecked();
    await page.getByRole("button", { name: "测试连接" }).click();
    await expect(page.getByText("连接正常")).toBeVisible();
    await page.getByRole("button", { name: "锁定保险库" }).click();
    await app.close();

    const inspection = spawnSync(
      path.resolve("../.venv/bin/python"),
      [
        "-c",
        "import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('select record_json from model_provider_profiles').fetchone()[0])",
        path.join(runtime, "workbench.sqlite"),
      ],
      { encoding: "utf8" },
    );
    expect(inspection.status).toBe(0);
    const record = JSON.parse(inspection.stdout.trim());
    expect(record.protocol).toBe("lmstudio");
    expect(record.enabled).toBe(true);
    expect(record.model_aliases.default).toBe("second-model");
    expect(record.secret_id).toMatch(/^provider\/[a-f0-9]{32}$/);
    expect(inspection.stdout).not.toContain(password);
    expect(inspection.stdout).not.toContain(credential);

    for (const file of await runtimeFiles(runtime)) {
      const bytes = await readFile(file);
      expect(bytes.includes(Buffer.from(password))).toBeFalsy();
      expect(bytes.includes(Buffer.from(credential))).toBeFalsy();
    }
  } finally {
    try { await app.close(); } catch { /* Already closed by the lifecycle assertions. */ }
    await upstream.close();
  }
});

test("explicitly recovers an incomplete vault through the real UI", async ({}, testInfo) => {
  const runtime = testInfo.outputPath("recovery-runtime");
  await mkdir(runtime, { recursive: true });
  await writeFile(path.join(runtime, "credentials.vault"), "");
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(runtime),
  });
  try {
    const page = await app.firstWindow();
    await page.getByRole("link", { name: "模型供应商" }).click();
    await expect(page.getByText("保险库需要恢复")).toBeVisible();
    await page.getByLabel("主密码").fill(`runtime-${randomUUID()}`);
    await page.getByRole("button", { name: "恢复并创建" }).click();
    await expect(page.getByRole("button", { name: "锁定保险库" })).toBeVisible();
  } finally {
    await app.close();
  }
});

import { _electron as electron, expect, test } from "@playwright/test";
import { createServer, type Server } from "node:http";
import { randomUUID } from "node:crypto";
import path from "node:path";

type RequestRecord = { method: string; path: string; body: Record<string, unknown> | null };

async function fakeApi(delayInitialVault = false, unconfirmedDelete = false) {
  let vaultStatus = "uninitialized";
  let providers: any[] = [];
  const requests: RequestRecord[] = [];
  let delayedSecret: (() => void) | undefined;
  let delayedVault: (() => void) | undefined;
  const server: Server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null;
    const method = request.method ?? "GET";
    const pathname = new URL(request.url ?? "/", "http://localhost").pathname;
    requests.push({ method, path: pathname, body });
    const send = (value: unknown, status = 200) => { response.writeHead(status, { "content-type": "application/json" }); response.end(JSON.stringify(value)); };
    if (pathname === "/api/vault/status") return send({ status: vaultStatus });
    if (pathname === "/api/vault/create" || pathname === "/api/vault/unlock") {
      if (delayInitialVault) { delayInitialVault = false; await new Promise<void>((resolve) => { delayedVault = resolve; }); }
      vaultStatus = "unlocked";
      return send({ status: vaultStatus });
    }
    if (pathname === "/api/vault/lock") { vaultStatus = "locked"; return send({ status: vaultStatus }); }
    if (pathname === "/api/providers" && method === "GET") return send(providers);
    if (pathname === "/api/providers" && method === "POST") {
      const input = body!;
      const profile = { ...input, headers: {}, credential_status: input.protocol === "lmstudio" ? "missing" : "configured" };
      providers = [...providers.filter((value) => value.id !== profile.id), profile];
      return send(profile, 201);
    }
    const id = pathname.split("/")[3];
    if (pathname.endsWith("/secret")) {
      if ((body as any)?.value) await new Promise<void>((resolve) => { delayedSecret = resolve; });
      return send({ id, credential_status: "configured" });
    }
    if (pathname.endsWith("/models")) return send({ status: "online", models: ["first-model", "second-model"], error_code: null });
    if (pathname.endsWith("/test")) return send({ status: "online", latency_ms: 12, models: ["first-model", "second-model"], error_code: null });
    if (method === "DELETE") { providers = providers.filter((value) => value.id !== id); return send({ id, status: "deleted", secret_cleanup: unconfirmedDelete ? "unconfirmed" : "confirmed" }, unconfirmedDelete ? 202 : 200); }
    return send({ detail: "not found" }, 404);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fake API did not bind");
  return {
    base: `http://127.0.0.1:${address.port}`,
    requests,
    releaseSecret: () => delayedSecret?.(),
    releaseVault: () => delayedVault?.(),
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

test("uses the isolated IPC bridge and rejects unapproved local API requests", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  const page = await app.firstWindow();
  expect(await page.evaluate(() => typeof (window as any).workbenchBridge.apiRequest)).toBe("function");
  await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "PUT", path: "/providers" }))).rejects.toThrow();
  await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/health" }))).rejects.toThrow();
  await expect(page.evaluate(() => (window as any).workbenchBridge.apiRequest({ method: "DELETE", path: "/providers/not.valid" }))).rejects.toThrow();
  await app.close();
});

test("creates, locks, unlocks, edits, tests, selects and deletes providers without retaining secrets", async () => {
  const api = await fakeApi(true);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_API_BASE: api.base } });
  const page = await app.firstWindow();
  const password = `runtime-${randomUUID()}`;
  const key = `runtime-${randomUUID()}`;
  page.on("dialog", (dialog) => void dialog.accept());

  await page.getByRole("link", { name: "模型供应商" }).click();
  await page.getByLabel("主密码").fill(password);
  await page.getByRole("button", { name: "创建并解锁" }).click();
  await expect(page.getByLabel("主密码")).toHaveValue("");
  await expect.poll(() => api.requests.some((request) => request.path === "/api/vault/create" && request.body?.password === password)).toBeTruthy();
  api.releaseVault();
  await page.getByRole("button", { name: "使用 LM Studio" }).click();
  await page.getByRole("button", { name: "保存供应商" }).click();
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByText("连接正常")).toBeVisible();
  await page.getByRole("button", { name: "发现模型" }).click();
  await page.getByRole("combobox", { name: "默认模型" }).selectOption("second-model");
  await expect.poll(() => api.requests.some((request) => request.method === "POST" && request.path === "/api/providers" && request.body?.model_aliases && (request.body.model_aliases as any).default === "second-model")).toBeTruthy();
  expect(api.requests.filter((request) => request.path === "/api/providers").every((request) => !("value" in (request.body ?? {})))).toBeTruthy();
  await page.getByRole("button", { name: "锁定保险库" }).click();
  await expect(page.getByLabel("主密码")).toBeVisible();
  await page.getByLabel("主密码").fill(password);
  await page.getByRole("button", { name: "解锁" }).click();
  await expect(page.getByRole("combobox", { name: "默认模型" })).toHaveValue("second-model");

  await page.getByRole("button", { name: "使用 DeepSeek" }).click();
  await page.getByLabel("基础地址").fill("https://api.deepseek.example");
  await page.getByLabel("API 密钥").fill(key);
  await page.getByRole("button", { name: "保存供应商" }).click();
  await expect(page.getByLabel("API 密钥")).toHaveValue("");
  await expect.poll(() => api.requests.some((request) => request.path.endsWith("/secret") && request.body?.value === key)).toBeTruthy();
  api.releaseSecret();
  await expect(page.getByText("供应商已保存")).toBeVisible();
  expect(api.requests.filter((request) => request.path.endsWith("/secret")).every((request) => Object.keys(request.body ?? {}).join(",") === "value")).toBeTruthy();
  await expect.poll(() => page.evaluate(() => `${document.body.innerText}|${localStorage.length}|${sessionStorage.length}`.includes("runtime-"))).toBeFalsy();

  await page.getByRole("button", { name: "删除供应商" }).click();
  await expect(page.getByText("供应商已删除")).toBeVisible();
  expect(api.requests.some((request) => request.method === "DELETE" && request.path === "/api/providers/deepseek-primary")).toBeTruthy();
  await app.close();
  await api.close();
});

test("shows a durability warning after a 202 provider delete and clears selection", async () => {
  const api = await fakeApi(false, true);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_API_BASE: api.base } });
  const page = await app.firstWindow();
  page.on("dialog", (dialog) => void dialog.accept());

  await page.getByRole("link", { name: "模型供应商" }).click();
  await page.getByLabel("主密码").fill(`runtime-${randomUUID()}`);
  await page.getByRole("button", { name: "创建并解锁" }).click();
  await page.getByRole("button", { name: "保存供应商" }).click();
  await page.getByRole("button", { name: "删除供应商" }).click();

  await expect(page.getByText("供应商元数据已删除；凭据删除耐久性未确认")).toBeVisible();
  await expect(page.getByRole("button", { name: "删除供应商" })).toHaveCount(0);
  expect(await page.evaluate(() => `${document.body.innerText}|${localStorage.length}|${sessionStorage.length}`.includes("runtime-"))).toBeFalsy();
  await app.close();
  await api.close();
});

import { _electron as electron, expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import path from "node:path";

test("unlocks vault and selects an LM Studio model", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  const page = await app.firstWindow();
  let vaultStatus = "locked";

  await page.route("http://127.0.0.1:8765/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(body),
    });

    if (url.pathname === "/api/vault/status") return json({ status: vaultStatus });
    if (url.pathname === "/api/vault/unlock") { vaultStatus = "unlocked"; return json({ status: vaultStatus }); }
    if (url.pathname === "/api/providers" && request.method() === "GET") return json([]);
    if (url.pathname === "/api/providers/lmstudio/test") {
      return json({ status: "online", latency_ms: 12, models: ["qwen2.5"], error_code: null });
    }
    if (url.pathname === "/api/providers/lmstudio/models") {
      return json({ status: "online", models: ["qwen2.5"], error_code: null });
    }
    return json({ id: "lmstudio", credential_status: "configured" }, 201);
  });

  await page.getByRole("link", { name: "模型供应商" }).click();
  await page.getByLabel("主密码").fill(`runtime-${randomUUID()}`);
  await page.getByRole("button", { name: "解锁" }).click();
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByText("连接正常")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "默认模型" })).toHaveValue("qwen2.5");

  await app.close();
});

test("discovers models from a saved provider without exposing credentials", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  const page = await app.firstWindow();
  const provider = {
    id: "lmstudio", name: "LM Studio", protocol: "lmstudio", base_url: "http://127.0.0.1:1234",
    headers: {}, model_aliases: {}, capabilities: ["streaming"], thinking_enabled: false,
    reasoning_effort: "high", credential_status: "missing",
  };

  await page.route("http://127.0.0.1:8765/api/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown) => route.fulfill({
      contentType: "application/json", headers: { "access-control-allow-origin": "*" }, body: JSON.stringify(body),
    });
    if (url.pathname === "/api/vault/status") return json({ status: "unlocked" });
    if (url.pathname === "/api/providers") return json([provider]);
    if (url.pathname === "/api/providers/lmstudio/models") return json({ status: "online", models: ["qwen2.5", "llama-3.2"], error_code: null });
    return json(provider);
  });

  await page.getByRole("link", { name: "模型供应商" }).click();
  await page.getByRole("button", { name: "发现模型" }).click();
  await expect(page.getByRole("combobox", { name: "默认模型" })).toHaveValue("qwen2.5");
  expect(await page.evaluate(() => localStorage.length)).toBe(0);

  await app.close();
});

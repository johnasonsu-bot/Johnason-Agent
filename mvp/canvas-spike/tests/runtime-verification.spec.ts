import { _electron as electron, expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { chmod, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

type Mode = "success" | "failed" | "timed_out" | "running" | "unavailable" | "poll_error" | "poll_missing" | "stale_cancel_success" | "stale_cancel_error";

// Only the local HTTP service is a test double. The Electron window, React forms,
// navigation and IPC transport are real. No external provider is called.
async function launchFixture(root: string, mode: Mode, profileMode = "valid", reasonCode?: string) {
  await mkdir(root, { recursive: true });
  const executable = path.join(root, "verification-service.mjs");
  const source = `#!/usr/bin/env node
import http from "node:http";
const mode = ${JSON.stringify(mode)};
const profileMode = ${JSON.stringify(profileMode)};
const reasonCode = ${JSON.stringify(reasonCode ?? null)};
let bootstrap = "", started = false, starts = 0, polls = 0, cancelled = false;
let accepted = null;
let releaseOldCancel = null, secondJobPolls = 0, cancelledJobId = null;
const staleCancelMode = mode.startsWith("stale_cancel_");
const profile = { id: "deepseek-primary", name: "DeepSeek", protocol: "deepseek", headers: {}, base_url: "https://api.deepseek.invalid", model_aliases: { default: "saved-deepseek-model" }, enabled: true, credential_mode: "reference", credential_status: "configured", capabilities: [], thinking_enabled: true, reasoning_effort: "high" };
if (profileMode === "disabled") profile.enabled = false;
if (profileMode === "no-model") profile.model_aliases = {};
if (profileMode === "incompatible") profile.protocol = "openai-compatible";
if (profileMode === "missing-secret") profile.credential_status = "missing";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => {
  bootstrap += chunk;
  if (started || !bootstrap.includes("\\n")) return;
  started = true;
  const identity = JSON.parse(bootstrap.slice(0, bootstrap.indexOf("\\n")));
  const server = http.createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    const send = (status, value) => { response.writeHead(status, { "content-type": "application/json" }); response.end(JSON.stringify(value)); };
    const job = (status, id = "verification-1") => ({ id, status, runtime_id: "dsh", provider_profile_id: profile.id, model: "saved-deepseek-model", ...(status === "failed" && reasonCode ? {reason_code: reasonCode} : {}), message: "safe summary", raw_body: "RAW_RESPONSE_MUST_NOT_RENDER" });
    if (pathname === "/api/health") return send(200, { status: "ok", service: "hermes-workbench", instance_id: identity.instance_id, port: server.address().port });
    if (pathname === "/api/vault/status") return send(200, { status: "unlocked" });
    if (pathname === "/api/vault/lock") return send(200, { status: "locked" });
    if (pathname === "/api/providers") return send(200, profileMode === "empty" ? [] : [profile]);
    if (pathname === "/api/agents") return send(200, []);
    if (pathname === "/api/engine-host/status") return send(200, { enabled: false, state: "disabled", protocol: null, capabilities: null, runner_mode: "python" });
    if (pathname === "/api/v1/engine-host") return send(200, { v2: { enabled: false, protocol: "2.0", runtimes: [] } });
    if (pathname === "/api/runtime-verifications" && request.method === "POST") {
      starts += 1;
      const value = JSON.parse(body);
      // Remember only whether the contract matched; never store the password.
      accepted = { runtime_id: value.runtime_id, provider_profile_id: value.provider_profile_id, hasPassword: typeof value.vault_password === "string" && value.vault_password.length > 0, keys: Object.keys(value).sort() };
      if (mode === "unavailable") return send(503, { detail: "verification_unavailable", raw_body: "RAW_RESPONSE_MUST_NOT_RENDER" });
      const id = staleCancelMode ? "verification-" + starts : "verification-1";
      return setTimeout(() => send(202, job("running", id)), 150);
    }
    if (pathname === "/api/providers/release-old-cancel/models") { releaseOldCancel?.(); return send(200, {}); }
    if (staleCancelMode && pathname === "/api/runtime-verifications/verification-1/cancel") {
      releaseOldCancel = () => mode === "stale_cancel_error" ? send(503, { detail: "verification_unavailable" }) : send(200, job("cancelled"));
      return;
    }
    if (staleCancelMode && pathname === "/api/runtime-verifications/verification-2") { secondJobPolls += 1; return send(200, job("running", "verification-2")); }
    if (staleCancelMode && pathname === "/api/runtime-verifications/verification-2/cancel") { cancelledJobId = "verification-2"; return send(200, job("cancelled", "verification-2")); }
    if (pathname === "/api/runtime-verifications/verification-1/cancel") { cancelled = true; return send(200, job("cancelled")); }
    if (pathname === "/api/runtime-verifications/verification-1") {
      polls += 1;
      if (staleCancelMode) return releaseOldCancel ? send(404, { detail: "verification_not_found" }) : send(200, job("running"));
      if (mode === "poll_error") return send(503, { detail: "verification_unavailable" });
      if (mode === "poll_missing") return send(404, { detail: "verification_not_found" });
      return send(200, job(cancelled ? "cancelled" : mode === "running" || polls < 2 ? "running" : mode === "success" ? "succeeded" : mode));
    }
    if (pathname === "/api/providers/verification-test-stats/models") return send(200, { starts, polls, cancelled, accepted, secondJobPolls, cancelledJobId });
    if (pathname.startsWith("/api/sessions")) return send(200, { session_id: "ui-session-0" });
    return send(404, { detail: "not found" });
  });
  server.listen(0, "127.0.0.1", () => console.log(JSON.stringify({ service: "hermes-workbench", instance_id: identity.instance_id, port: server.address().port })));
});
`;
  await writeFile(executable, source, "utf8");
  await chmod(executable, 0o755);
  const app = await electron.launch({ args: [path.resolve(".")], env: { ...process.env, HERMES_PYTHON: executable, HERMES_RUNTIME_DIR: path.join(root, "runtime") } });
  const page = await app.firstWindow();
  await page.getByRole("link", { name: "模型供应商" }).click();
  return { app, page, panel: page.getByRole("region", { name: "DeepSeek Harness 人工验收" }) };
}

async function stats(page: Page) {
  return page.evaluate(async () => (await (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/providers/verification-test-stats/models" })).body);
}

for (const [code, explanation] of [
  ["vault_in_use", "保险库被其他进程占用"],
  ["vault_unlock_failed", "保险库密码校验失败"],
  ["provider_request_failed", "模型 API 请求失败"],
  ["runtime_build_unavailable", "本地运行时构建不可用"],
  ["RAW_REASON_MUST_NOT_RENDER", "未返回可识别的失败原因"],
] as const) {
  test(`verification explains ${code} without blaming unrelated passwords`, async ({}, info) => {
    const { app, panel } = await launchFixture(info.outputPath("backend"), "failed", "valid", code);
    try {
      await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
      await panel.getByRole("button", { name: "开始真实验收" }).click();
      await expect(panel).toContainText(explanation, { timeout: 10000 });
      await expect(panel).not.toContainText("请检查 Vault 密码、已保存凭据、模型及本地验收环境");
      await expect(panel).not.toContainText("RAW_REASON_MUST_NOT_RENDER");
      await expect(panel).not.toContainText("RAW_RESPONSE_MUST_NOT_RENDER");
    } finally { await app.close(); }
  });
}

test("explicit verification uses the saved profile and clears the one-time password before the response", async ({}, testInfo) => {
  const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), "success");
  try {
    await expect(panel).toBeVisible();
    await expect(panel.getByLabel("验收供应商")).toHaveValue("deepseek-primary");
    await expect(panel.getByLabel("已保存的验收模型")).toHaveValue("saved-deepseek-model");
    await expect(panel.getByLabel("已保存的验收模型")).toHaveAttribute("readonly", "");
    await expect(panel).toContainText("真实 API 费用");
    await expect(panel).toContainText("测试连接不等于 Harness 验收");
    expect((await stats(page)).starts).toBe(0);
    const password = randomUUID();
    const input = panel.getByLabel("本次验收 Vault 密码");
    await expect(input).toHaveAttribute("type", "password");
    await input.fill(password);
    await panel.getByRole("button", { name: "开始真实验收" }).click();
    await expect(input).toHaveValue("");
    await expect(panel.getByRole("button", { name: "正在启动…" })).toBeDisabled();
    await expect(panel).toContainText("验收运行中");
    await expect(panel.getByText("验收通过", { exact: true })).toHaveCount(0);
    await expect(panel.getByText("验收通过", { exact: true })).toBeVisible({ timeout: 10000 });
    expect(await stats(page)).toMatchObject({ starts: 1, accepted: { runtime_id: "dsh", provider_profile_id: "deepseek-primary", hasPassword: true, keys: ["provider_profile_id", "runtime_id", "vault_password"] } });
    const visible = await page.evaluate(() => ({ html: document.body.innerHTML, local: JSON.stringify(localStorage), session: JSON.stringify(sessionStorage) }));
    expect(JSON.stringify(visible)).not.toContain(password);
    expect(visible.html).not.toContain("RAW_RESPONSE_MUST_NOT_RENDER");
    await expect(panel).toContainText("不会自动更新运行时准入");
    await panel.screenshot({ path: testInfo.outputPath("verification-panel.png") });
  } finally { await app.close(); }
});

for (const mode of ["empty", "disabled", "no-model", "incompatible", "missing-secret"]) {
  test(`verification cannot start with ${mode} saved provider`, async ({}, testInfo) => {
    const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), "running", mode);
    try {
      await expect(panel).toBeVisible();
      await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
      await expect(panel.getByRole("button", { name: "开始真实验收" })).toBeDisabled();
      expect((await stats(page)).starts).toBe(0);
    } finally { await app.close(); }
  });
}

for (const [mode, text] of [["failed", "验收失败"], ["timed_out", "验收超时"], ["unavailable", "验收服务暂不可用"]] as const) {
  test(`${mode} is shown safely and requires a fresh password to retry`, async ({}, testInfo) => {
    const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), mode);
    try {
      await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
      await panel.getByRole("button", { name: "开始真实验收" }).click();
      await expect(panel).toContainText(text, { timeout: 10000 });
      await expect(panel).not.toContainText("RAW_RESPONSE_MUST_NOT_RENDER");
      const retry = panel.getByRole("button", { name: "重新验收" });
      await expect(retry).toBeDisabled();
      await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
      await expect(retry).toBeEnabled();
      await retry.click();
      await expect.poll(async () => (await stats(page)).starts).toBe(2);
      await expect(panel.getByLabel("本次验收 Vault 密码")).toHaveValue("");
    } finally { await app.close(); }
  });
}

test("navigation resumes viewing the same job without resubmitting and cancellation ends polling", async ({}, testInfo) => {
  const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), "running");
  try {
    await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
    await panel.getByRole("button", { name: "开始真实验收" }).click();
    await expect(panel).toContainText("验收运行中");
    await expect(panel).toContainText("离开此页面不会取消验收");
    await page.getByRole("button", { name: "主页" }).click();
    await page.getByRole("link", { name: "模型供应商" }).click();
    await expect(panel).toContainText("验收运行中");
    expect((await stats(page)).starts).toBe(1);
    await panel.getByRole("button", { name: "取消验收" }).click();
    await expect(panel).toContainText("验收已取消");
    expect((await stats(page)).cancelled).toBe(true);
  } finally { await app.close(); }
});

test("a missing verification record releases the run lock without claiming success", async ({}, testInfo) => {
  const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), "poll_missing");
  try {
    await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
    await panel.getByRole("button", { name: "开始真实验收" }).click();
    await expect(panel).toContainText("验收记录已失效", { timeout: 10000 });
    await expect(panel).toContainText("结果未知");
    await expect(panel.getByText("验收通过", { exact: true })).toHaveCount(0);
    await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
    await panel.getByRole("button", { name: "重新验收" }).click();
    await expect.poll(async () => (await stats(page)).starts).toBe(2);
  } finally { await app.close(); }
});

test("poll failure does not report the accepted job as completed or allow duplicate starts", async ({}, testInfo) => {
  const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), "poll_error");
  try {
    await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
    await panel.getByRole("button", { name: "开始真实验收" }).click();
    await expect(panel).toContainText("暂时无法读取验收状态", { timeout: 10000 });
    await expect(panel.getByText("验收通过", { exact: true })).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "验收进行中" })).toBeDisabled();
    expect((await stats(page)).starts).toBe(1);
    await panel.getByRole("button", { name: "取消验收" }).click();
    await expect(panel).toContainText("验收已取消");
  } finally { await app.close(); }
});

for (const mode of ["stale_cancel_success", "stale_cancel_error"] as const) {
  test(`${mode} cannot replace or disable a new job after the old record disappears`, async ({}, testInfo) => {
    const { app, page, panel } = await launchFixture(testInfo.outputPath("backend"), mode);
    try {
      await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
      await panel.getByRole("button", { name: "开始真实验收" }).click();
      await expect(panel).toContainText("验收运行中");
      await panel.getByRole("button", { name: "取消验收" }).click();
      await expect(panel).toContainText("验收记录已失效");
      await panel.getByLabel("本次验收 Vault 密码").fill(randomUUID());
      await panel.getByRole("button", { name: "重新验收" }).click();
      await expect(panel).toContainText("验收编号：verification-2");
      await expect.poll(async () => (await stats(page)).secondJobPolls).toBeGreaterThan(0);
      // Record observable renderer transitions while releasing the old response;
      // even a transient old error/result is a regression, not just the final UI.
      await page.evaluate(() => {
        (window as any).__verificationTransitions = [];
        const section = document.getElementById("runtime-verification-title")!.closest("section")!;
        new MutationObserver(() => (window as any).__verificationTransitions.push(section.textContent)).observe(section, { childList: true, subtree: true, characterData: true });
      });
      const before = (await stats(page)).secondJobPolls;
      await page.evaluate(async () => { await (window as any).workbenchBridge.apiRequest({ method: "GET", path: "/providers/release-old-cancel/models" }); });
      await expect.poll(async () => (await stats(page)).secondJobPolls).toBeGreaterThan(before);
      await expect(panel).toContainText("验收编号：verification-2");
      await expect(panel.getByRole("button", { name: "取消验收" })).toBeEnabled();
      const transitions = await page.evaluate(() => (window as any).__verificationTransitions.join("\n"));
      expect(transitions).not.toContain("验收编号：verification-1");
      expect(transitions).not.toContain("验收服务暂不可用");
      await panel.getByRole("button", { name: "取消验收" }).click();
      await expect(panel).toContainText("验收已取消");
      expect(await stats(page)).toMatchObject({ starts: 2, cancelledJobId: "verification-2" });
    } finally { await app.close(); }
  });
}

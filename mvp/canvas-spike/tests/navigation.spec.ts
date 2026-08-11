import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("switches between V4 navigation pages without opening a modal by default", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await expect(page.getByRole("dialog", { name: "新建会话" })).toHaveCount(0);
    await page.getByRole("button", { name: "Workspace" }).click();
    await expect(page.getByRole("heading", { name: /Workspace · 工作空间/ })).toBeVisible();
    await page.getByRole("button", { name: "本地" }).click();
    await expect(page.getByText("本地项目 · generic-agent")).toBeVisible();
    await expect(page.getByText("项目云端 · Data Platform")).toBeHidden();
    await page.getByRole("button", { name: "返回会话" }).click();
    await expect(page.getByRole("heading", { name: /会话 · Conversations/ })).toBeVisible();
  } finally {
    await app.close();
  }
});

test("opens Agent configuration for cross-model routing", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "Agent 配置" }).click();
    await expect(page.getByRole("heading", { name: "Agent 配置 · Agent routing" })).toBeVisible();
    await expect(page.getByLabel("产品经理 Provider")).toBeVisible();
    await expect(page.getByLabel("产品经理 Model")).toBeVisible();
    await page.getByLabel("产品经理 Provider").selectOption("deepseek");
    await page.getByLabel("产品经理 Model").fill("deepseek-v4-flash");
    await page.getByRole("button", { name: "保存 Agent 配置" }).click();
    await expect(page.getByText("Agent 配置已保存")).toBeVisible();
  } finally {
    await app.close();
  }
});

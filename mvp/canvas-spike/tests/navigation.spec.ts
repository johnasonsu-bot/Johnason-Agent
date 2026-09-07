import { expect, test } from "@playwright/test";
import path from "node:path";
import { launchTestElectron } from "./support/electron-launch";

test("switches between V4 navigation pages without opening a modal by default", async () => {
  const app = await launchTestElectron({ args: [path.resolve(".")] });
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
  const app = await launchTestElectron({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "Agent 配置" }).click();
    await expect(page.getByRole("heading", { name: "Agent 配置 · Agent routing" })).toBeVisible();
    await expect(page.getByText("运行模式仅作为已保存配置，尚未接入节点执行；接线将在 R4 集成后生效。")).toBeVisible();
    await expect(page.getByLabel("产品经理 Provider")).toBeVisible();
    await expect(page.getByLabel("产品经理 Model")).toBeVisible();
    const runtime = page.getByLabel("产品经理 运行模式");
    await expect(runtime).toBeVisible();
    await expect(runtime.locator("option")).toHaveText([
      "请选择运行模式",
      "Agent-步进执行模式（Codex Harness）",
      "Agent-寻路模式（Claude Harness）",
      "Agent-事件驱动模式（DeepSeek Harness）",
    ]);
    await page.getByRole("button", { name: "保存 Agent 配置" }).click();
    await expect(page.getByRole("status")).toContainText("请为产品经理明确选择运行模式");
    await runtime.selectOption("goose");
    await expect(runtime).toHaveValue("goose");
    for (const selector of await page.getByRole("combobox", { name: /运行模式/ }).all()) {
      await selector.selectOption("goose");
    }
    await page.getByLabel("产品经理 Provider").selectOption("deepseek");
    await page.getByLabel("产品经理 Model").fill("deepseek-v4-flash");
    await page.getByRole("button", { name: "保存 Agent 配置" }).click();
    await expect(page.getByRole("status")).toContainText(/Agent 配置(已保存|保存失败)/);
  } finally {
    await app.close();
  }
});

import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("opens the V4 conversation workbench with composer and artifacts context", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await expect(page.getByRole("button", { name: "◌ 会话" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: /会话 · Conversations/ })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "会话消息" })).toBeVisible();
    await expect(page.getByRole("complementary", { name: "智能画布 · Artifacts" })).toBeVisible();
    await expect(page.getByTestId("conversation-source")).toBeVisible();
    await expect(page.getByLabel("当前模型")).toBeVisible();
  } finally {
    await app.close();
  }
});

test("creates a multi-agent session from the picker", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "新建会话" }).click();
    await expect(page.getByRole("dialog", { name: "新建会话" })).toBeVisible();
    await page.getByRole("button", { name: "多 Agent" }).click();
    const dialog = page.getByRole("dialog", { name: "新建会话" });
    await dialog.getByRole("button", { name: "产品经理", exact: true }).click();
    await dialog.getByRole("button", { name: "架构师", exact: true }).click();
    await dialog.getByRole("button", { name: "创建会话", exact: true }).click();
    await expect(page.getByTestId("agent-avatar-stack")).toBeVisible();
    await expect(page.getByTestId("conversation-title")).toHaveText(/产品经理、架构师/);
  } finally {
    await app.close();
  }
});

test("inserts one mention token and adds context to the composer", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    const input = page.getByRole("textbox", { name: "会话消息" });
    await input.fill("请检查 ");
    await page.getByRole("button", { name: "提及 Agent、Skill 或 Tool" }).click();
    await page.getByRole("button", { name: "@架构师 · Agent" }).click();
    await expect(input).toHaveValue("请检查 @架构师 ");
    await page.getByRole("button", { name: "添加到本轮上下文" }).click();
    await page.getByRole("button", { name: "文件 · report.pdf" }).click();
    await expect(page.getByTestId("context-chips")).toContainText("文件 · report.pdf");
  } finally {
    await app.close();
  }
});

test("keeps the user turn visible while the Task3 request runs", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    const input = page.getByRole("textbox", { name: "会话消息" });
    await input.fill("列出当前工作区的关键文件");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText("列出当前工作区的关键文件", { exact: true }).last()).toBeVisible();
    await expect(page.getByTestId("conversation-status")).toContainText(/排队中|执行中|完成|替身|失败/);
  } finally {
    await app.close();
  }
});

test("supports the story-to-animation multi-agent scenario", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    const scenario = "@产品经理 写一篇200字小说 @架构师 改写成一个动画html";
    const input = page.getByRole("textbox", { name: "会话消息" });
    await input.fill(scenario);
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText(scenario, { exact: true }).last()).toBeVisible();
    await expect(page.getByTestId("conversation-status")).toContainText(/排队中|执行中|等待重试|完成|失败/);
  } finally {
    await app.close();
  }
});

test("keeps a newly created session in the sidebar after reloading the workbench", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "新建会话" }).click();
    const dialog = page.getByRole("dialog", { name: "新建会话" });
    await dialog.getByRole("button", { name: "产品经理", exact: true }).click();
    await dialog.getByRole("button", { name: "创建会话", exact: true }).click();
    await expect(page.getByTestId("conversation-title")).toHaveText("产品经理 · 新任务");
    await page.reload();
    await expect(page.getByText("产品经理 · 新任务", { exact: true }).first()).toBeVisible();
  } finally {
    await app.close();
  }
});

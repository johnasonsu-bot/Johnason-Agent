import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("sends a prompt and shows model, steps, and answer", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("link", { name: "会话" }).click();

    await page.getByPlaceholder("输入消息或介入要求").fill("列出项目文件");
    await page.getByRole("button", { name: "发送" }).click();

    await expect(page.getByTestId("model-badge")).toContainText(/LM Studio|DeepSeek/);
    await expect(page.getByTestId("conversation-source")).toContainText(/Task 3 REST\/SSE|fixture/);
    await expect(page.getByText("工具执行完成")).toBeVisible();
    await expect(page.getByText(/README\.md/)).toBeVisible();
  } finally {
    await app.close();
  }
});

test("creates a group conversation from three selected agents", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("link", { name: "会话" }).click();

    await page.getByRole("button", { name: "选择 Agent" }).click();
    const picker = page.getByRole("dialog", { name: "选择多个 Agent" });
    await picker.getByLabel("产品经理").check();
    await picker.getByLabel("架构师").check();
    await picker.getByLabel("工程师", { exact: true }).check();
    await page.getByRole("button", { name: "创建群聊" }).click();

    await expect(page.getByTestId("agent-avatar-stack")).toContainText("+1");
    await expect(page.getByText("产品经理 · Product Manager")).toBeVisible();
    await expect(page.getByText("架构师 · Architect")).toBeVisible();
  } finally {
    await app.close();
  }
});

test("creates and selects a new durable group conversation", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("link", { name: "会话" }).click();

    await page.getByRole("button", { name: "选择 Agent" }).click();
    const picker = page.getByRole("dialog", { name: "选择多个 Agent" });
    await picker.getByLabel("产品经理").check();
    await picker.getByLabel("架构师").check();
    await picker.getByLabel("工程师", { exact: true }).check();
    await page.getByRole("button", { name: "创建群聊" }).click();

    await expect(page.getByRole("button", { name: /多 Agent 会话/ })).toBeVisible();
    await expect(page.getByTestId("conversation-source")).toContainText(/session ui-group-/);
  } finally {
    await app.close();
  }
});

test("switches the current role from the sidebar menu", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("link", { name: "会话" }).click();
    await page.getByRole("button", { name: /当前角色/ }).click();
    await page.getByRole("menuitem", { name: "测试工程师" }).click();

    await expect(page.getByRole("button", { name: /测试工程师/ })).toBeVisible();
    await page.getByRole("button", { name: /测试工程师/ }).click();
    await expect(page.getByRole("menuitem", { name: "角色市场" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "配置当前 Claw" })).toBeVisible();
  } finally {
    await app.close();
  }
});

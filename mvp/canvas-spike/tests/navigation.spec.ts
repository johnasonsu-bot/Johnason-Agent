import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("uses the left navigation to switch and restore workspace views", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    const workspaceNavigation = page.getByRole("navigation", { name: "工作区" });

    await expect(workspaceNavigation.getByRole("link")).toHaveText([
      "主页", "会话", "Agent", "任务", "Artifacts",
    ]);

    await workspaceNavigation.getByRole("link", { name: "任务" }).click();
    await expect(page).toHaveURL(/#tasks$/);
    await expect(workspaceNavigation.getByRole("link", { name: "任务" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: "任务" })).toBeVisible();
    await expect(page.getByText("任务工作区正在建设中")).toBeVisible();

    await page.reload();
    await expect(workspaceNavigation.getByRole("link", { name: "任务" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: "任务" })).toBeVisible();

    await page.getByRole("navigation", { name: "设置" }).getByRole("link", { name: "模型供应商" }).click();
    await expect(page).toHaveURL(/#providers$/);
    await expect(page.getByRole("heading", { name: "模型供应商" })).toBeVisible();
    await expect(workspaceNavigation.getByRole("link", { name: "任务" })).toHaveAttribute("aria-current", "page");

    await workspaceNavigation.getByRole("link", { name: "Artifacts" }).click();
    await expect(page).toHaveURL(/#artifacts$/);
    await expect(page.getByTestId("artifact-markdown")).toBeVisible();
  } finally {
    await app.close();
  }
});

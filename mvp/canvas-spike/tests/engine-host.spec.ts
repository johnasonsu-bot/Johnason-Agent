import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

function ownedEnvironment(runtimeDir: string) {
  return {
    ...process.env,
    HERMES_PYTHON: path.resolve("../.venv/bin/python"),
    HERMES_RUNTIME_DIR: runtimeDir,
  };
}

test("shows the read-only Engine Host contract state", async ({}, testInfo) => {
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: ownedEnvironment(testInfo.outputPath("runtime")),
  });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "Agent 配置" }).click();
    const card = page.getByRole("region", { name: "Engine Host 状态" });
    await expect(card).toBeVisible();
    await expect(card).toContainText("Python Runtime");
    await expect(card).toContainText("disabled");
    await expect(card.getByRole("button", { name: "刷新 Engine Host 状态" })).toBeVisible();
    await expect(card.getByRole("textbox")).toHaveCount(0);
    await expect(card.getByRole("combobox")).toHaveCount(0);
  } finally {
    await app.close();
  }
});

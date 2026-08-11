import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("keeps V4 navigation and the provider center available without exposing Node", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await expect(page.getByRole("button", { name: "设置" })).toBeVisible();
    await page.getByRole("link", { name: "模型供应商" }).click();
    await expect(page.getByRole("heading", { name: "模型供应商" })).toBeVisible();
    expect(await page.evaluate(() => typeof (window as any).require)).toBe("undefined");
    expect(await page.evaluate(() => typeof (window as any).process)).toBe("undefined");
  } finally {
    await app.close();
  }
});

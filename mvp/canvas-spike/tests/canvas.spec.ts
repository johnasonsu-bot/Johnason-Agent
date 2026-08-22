import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("shows the Artifacts pane and keeps renderer isolation", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  const page = await app.firstWindow();

  await expect(page.getByRole("complementary", { name: "智能画布 · Artifacts" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run graph" })).toBeVisible();
  await expect(page.getByRole("button", { name: "折叠画布" })).toBeVisible();

  expect(await page.evaluate(() => typeof (window as any).require)).toBe("undefined");
  expect(await page.evaluate(() => typeof (window as any).process)).toBe("undefined");
  expect(await page.evaluate(() => (window as any).workbenchBridge.capabilities())).toEqual([
    "artifact.read",
    "intervention.submit",
  ]);

  await app.close();
});

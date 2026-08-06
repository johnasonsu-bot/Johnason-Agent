import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("renders artifacts without exposing Node or unrestricted IPC", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  const page = await app.firstWindow();

  await expect(page.getByTestId("artifact-markdown")).toContainText("Phase 0");
  await expect(page.getByTestId("artifact-chart")).toBeVisible();
  await expect(page.getByTestId("artifact-audio")).toBeVisible();
  await expect(page.getByTestId("artifact-unknown")).toContainText("No renderer");

  expect(await page.evaluate(() => typeof (window as any).require)).toBe("undefined");
  expect(await page.evaluate(() => typeof (window as any).process)).toBe("undefined");
  expect(await page.evaluate(() => (window as any).workbenchBridge.capabilities())).toEqual([
    "artifact.read",
  ]);

  const frame = page.getByTestId("artifact-html");
  expect(await frame.getAttribute("sandbox")).toBe("");
  expect(await page.getByTestId("artifact-audio").evaluate((node) => (node as HTMLAudioElement).autoplay)).toBe(false);

  await app.close();
});

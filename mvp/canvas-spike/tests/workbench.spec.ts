import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("shows artifact workbench renderers while preserving the sandbox", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  const page = await app.firstWindow();

  await expect(page.getByRole("link", { name: "Artifacts" })).toBeVisible();
  await expect(page.getByText("application/json")).toBeVisible();
  await expect(page.getByTestId("artifact-table")).toBeVisible();
  await expect(page.getByTestId("artifact-run-graph")).toBeVisible();
  expect(await page.evaluate(() => typeof (window as any).require)).toBe("undefined");
  expect(await page.evaluate(() => typeof (window as any).process)).toBe("undefined");
  const intervention = await page.evaluate(() => (window as any).workbenchBridge.submitIntervention({
    runId: "run-1",
    artifactId: "json",
    kind: "annotation",
    payload: { note: "verify this value" },
  }));
  expect(intervention).toEqual({ accepted: true, runId: "run-1", artifactId: "json" });

  await app.close();
});

import { expect, test } from "@playwright/test";
import { access } from "node:fs/promises";
import path from "node:path";
import { isolatedElectronEnvironment, launchTestElectron } from "./support/electron-launch";

async function exists(candidate: string): Promise<boolean> {
  try {
    await access(candidate);
    return true;
  } catch {
    return false;
  }
}

test("a default test launch ignores an inherited runtime directory", async ({}, testInfo) => {
  const poisonedRuntime = testInfo.outputPath("inherited-runtime");
  const isolationDirectory = testInfo.outputPath("isolated-launch");
  const previous = process.env.HERMES_RUNTIME_DIR;
  process.env.HERMES_RUNTIME_DIR = poisonedRuntime;
  const app = await launchTestElectron({ isolationDirectory });
  try {
    await app.firstWindow();
    await expect.poll(() => exists(path.join(isolationDirectory, "runtime", "workbench.sqlite"))).toBe(true);
    expect(await exists(path.join(poisonedRuntime, "workbench.sqlite"))).toBe(false);
  } finally {
    await app.close();
    if (previous === undefined) delete process.env.HERMES_RUNTIME_DIR;
    else process.env.HERMES_RUNTIME_DIR = previous;
  }
});

test("launch environment strips inherited live flags and credentials but accepts explicit overrides", ({}, testInfo) => {
  const environment = isolatedElectronEnvironment(testInfo.outputPath("contract"), {
    PATH: "/safe/bin",
    HERMES_RUNTIME_DIR: "/user/runtime",
    WORKBENCH_ENGINE_HOST_V2_ENABLED: "true",
    OPENAI_API_KEY: "user-secret",
  }, {
    WORKBENCH_ENGINE_HOST_V2_ENABLED: "false",
    HERMES_LMSTUDIO_BASE_URL: "http://127.0.0.1:4321",
  });
  expect(environment).toMatchObject({
    PATH: "/safe/bin",
    HERMES_RUNTIME_DIR: path.join(testInfo.outputPath("contract"), "runtime"),
    WORKBENCH_ENGINE_HOST_V2_ENABLED: "false",
    HERMES_LMSTUDIO_BASE_URL: "http://127.0.0.1:4321",
  });
  expect(environment.OPENAI_API_KEY).toBeUndefined();
});

test("an explicit override is preserved when it equals the inherited value", ({}, testInfo) => {
  const environment = isolatedElectronEnvironment(testInfo.outputPath("same-value"), {
    WORKBENCH_ENGINE_HOST_V2_ENABLED: "true",
  }, {
    WORKBENCH_ENGINE_HOST_V2_ENABLED: "true",
  });
  expect(environment.WORKBENCH_ENGINE_HOST_V2_ENABLED).toBe("true");
});

test("relaunching the same isolation directory preserves its Electron state", async ({}, testInfo) => {
  const isolationDirectory = testInfo.outputPath("restart");
  let app = await launchTestElectron({ isolationDirectory });
  const first = await app.firstWindow();
  await first.evaluate(() => localStorage.setItem("restart-marker", "present"));
  await app.close();

  app = await launchTestElectron({ isolationDirectory });
  try {
    const second = await app.firstWindow();
    expect(await second.evaluate(() => localStorage.getItem("restart-marker"))).toBe("present");
  } finally {
    await app.close();
  }
});

test("independent isolation directories do not share Electron state", async ({}, testInfo) => {
  const first = await launchTestElectron({ isolationDirectory: testInfo.outputPath("first") });
  const firstPage = await first.firstWindow();
  await firstPage.evaluate(() => localStorage.setItem("isolation-marker", "first"));
  await first.close();

  const second = await launchTestElectron({ isolationDirectory: testInfo.outputPath("second") });
  try {
    const secondPage = await second.firstWindow();
    expect(await secondPage.evaluate(() => localStorage.getItem("isolation-marker"))).toBeNull();
  } finally {
    await second.close();
  }
});

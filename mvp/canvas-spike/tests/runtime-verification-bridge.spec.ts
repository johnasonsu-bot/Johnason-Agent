import { _electron as electron, expect, test } from "@playwright/test";
import path from "node:path";

test("manual verification requests reach the owned backend without exposing arbitrary IPC routes", async ({}, testInfo) => {
  const app = await electron.launch({
    args: [path.resolve(".")],
    env: { ...process.env, HERMES_PYTHON: path.resolve("../.venv/bin/python"), HERMES_RUNTIME_DIR: testInfo.outputPath("runtime") },
  });
  try {
    const page = await app.firstWindow();
    const request = (method: string, route: string, body?: Record<string, unknown>) => page.evaluate(
      (args) => (window as any).workbenchBridge.apiRequest(args), { method, path: route, body },
    );
    // Missing profile never starts a model call; the backend, not IPC, reports it.
    await expect(request("POST", "/runtime-verifications", {
      runtime_id: "dsh", provider_profile_id: "does-not-exist", vault_password: "unused-test-input",
    })).resolves.toMatchObject({ status: 404, body: { detail: "provider_not_found" } });
    await expect(request("GET", "/runtime-verifications/absent-job")).resolves.toMatchObject({ status: 404, body: { detail: "verification_not_found" } });
    await expect(request("POST", "/runtime-verifications/absent-job/cancel")).resolves.toMatchObject({ status: 404, body: { detail: "verification_not_found" } });
    for (const [method, route] of [
      ["DELETE", "/runtime-verifications/absent-job"],
      ["GET", "/runtime-verifications/absent-job/cancel"],
      ["POST", "/runtime-verifications/absent-job"],
      ["GET", "/runtime-verifications/../providers"],
    ]) {
      await expect(request(method, route)).rejects.toThrow("invalid local API request");
    }
  } finally {
    await app.close();
  }
});

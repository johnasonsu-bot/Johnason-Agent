import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
test(
  "native Web profile loads trusted core, signs in, executes, and strips DB environment",
  { timeout: 40000 },
  async (t) => {
    const root = await mkdtemp(join(tmpdir(), "dp-native-web-")),
      corePath = join(root, "core.cjs");
    await writeFile(
      corePath,
      `exports.createCore=()=>{const captured=process.env.DB_PASSWORD;return {login:async()=>({token:'private-session'}),profile:async()=>({id:1}),execute:async({command})=>({command,captured:captured==='db-secret',removed:process.env.DB_PASSWORD===undefined}),logout:async()=>{},close:async()=>{}}}`,
    );
    const probe = createServer();
    await new Promise((r) => probe.listen(0, "127.0.0.1", r));
    const port = probe.address().port;
    await new Promise((r) => probe.close(r));
    const child = spawn(
      process.execPath,
      [
        fileURLToPath(new URL("../src/cli.mjs", import.meta.url)),
        "web",
        "--core",
        corePath,
        "--data-dir",
        root,
        "--port",
        String(port),
        "--no-open",
      ],
      {
        env: { PATH: process.env.PATH, HOME: root, DB_PASSWORD: "db-secret" },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let output = "";
    child.stdout.on("data", (b) => {
      output += b;
    });
    child.stderr.on("data", (b) => {
      output += b;
    });
    const exit = new Promise((r) =>
      child.once("exit", (code, signal) => r({ code, signal })),
    );
    t.after(async () => {
      if (child.exitCode === null) child.kill("SIGTERM");
      await exit;
    });
    const deadline = Date.now() + 30000;
    while (!output.includes(`dsh web: http://127.0.0.1:${port}`)) {
      assert.equal(child.exitCode, null, output);
      if (Date.now() > deadline) throw Error(output);
      await new Promise((r) => setTimeout(r, 50));
    }
    const origin = `http://127.0.0.1:${port}`,
      post = async (path, data) => {
        const r = await fetch(origin + path, {
          method: "POST",
          headers: { origin, "content-type": "application/json" },
          body: JSON.stringify(data),
        });
        return { status: r.status, data: await r.json() };
      };
    assert.equal((await fetch(origin + "/dataplatform")).status, 200);
    const { chromium } = await import("playwright-core");
    const browser = await chromium.launch({
      headless: true,
      executablePath:
        process.env.DSH_TEST_BROWSER_PATH ||
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    });
    t.after(() => browser.close());
    const page = await browser.newPage();
    await page.goto(origin + "/dataplatform");
    await page.waitForFunction(
      () => document.querySelector("#command").options.length > 0,
    );
    assert.match(await page.locator("#result").textContent(), /VAULT_LOCKED/);

    assert.equal(
      (
        await post("/dataplatform/login", {
          username: "alice",
          password: "platform-secret",
        })
      ).data.error,
      "VAULT_LOCKED",
    );
    assert.equal(
      (
        await post("/vault/initialize", {
          password: "master-secret",
          confirmation: "master-secret",
        })
      ).status,
      200,
    );
    await page.locator("#username").fill("alice");
    await page.locator("#password").fill("platform-secret");
    await page.locator("#login button").first().click();
    await page.waitForFunction(() =>
      document
        .querySelector("#result")
        .textContent.includes('"authorized": true'),
    );
    assert.equal(await page.locator("#password").inputValue(), "");
    await page.locator("#command").selectOption("project.list");
    await page.locator("#execute button").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#result")
        .textContent.includes('"captured": true'),
    );
    await browser.close();
    assert.deepEqual(
      (await post("/dataplatform/execute", { command: "project.list" })).data,
      { command: "project.list", captured: true, removed: true },
    );
    assert.doesNotMatch(
      await readFile(join(root, "standalone-web.patch.yml"), "utf8"),
      /db-secret|private-session|platform-secret|master-secret/,
    );
    assert.doesNotMatch(
      output,
      /db-secret|private-session|platform-secret|master-secret/,
    );
    child.kill("SIGTERM");
    assert.deepEqual(await exit, { code: 0, signal: null });
  },
);

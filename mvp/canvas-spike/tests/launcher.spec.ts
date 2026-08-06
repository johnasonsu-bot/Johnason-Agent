import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";

test("provides a launch check instead of requiring file URL access", () => {
  const output = execFileSync("npm", ["run", "start:check"], {
    cwd: process.cwd(),
    encoding: "utf8",
  });

  expect(output).toContain('"status":"ready"');
  expect(output).toContain('"runtime":"electron"');
});

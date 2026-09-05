import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { stripTypeScriptTypes } from "node:module";

// Evaluate the real guard without importing Electron or starting a client.
const source = readFileSync("src/main.ts", "utf8");
const guardSource = source.slice(source.indexOf("function isApiRequest("), source.indexOf("function sameTrustedDocument("));
const guard = vm.runInNewContext(stripTypeScriptTypes(`${guardSource}\nisApiRequest;`), { allowedApiRequests: new Set(["GET /api/v1/engine-host"]) });

test("event byte budget is explicitly bounded and restricted to the events route", () => {
  const request = { method: "GET", path: "/sessions/session-1/events" };
  expect(guard(request)).toBe(true);
  expect(guard({ ...request, headers: { "Last-Event-ID": "2:19", "X-Event-Page-Bytes": "262144" } })).toBe(true);
  expect(guard({ ...request, headers: { "X-Event-Page-Bytes": "1048576" } })).toBe(false);
  expect(guard({ ...request, headers: { Authorization: "not-a-secret" } })).toBe(false);
  expect(guard({ ...request, path: "/v1/engine-host", headers: { "X-Event-Page-Bytes": "262144" } })).toBe(false);
  expect(guard({ ...request, headers: { "Last-Event-ID": "2:19\r\nX: y" } })).toBe(false);
});

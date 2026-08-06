import { access } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const required = [
  "dist/index.html",
  "dist-electron/main.js",
  "dist-electron/preload.js",
];

await Promise.all(required.map((file) => access(path.resolve(file))));

if (process.argv.includes("--check")) {
  process.stdout.write(JSON.stringify({ status: "ready", runtime: "electron" }));
}

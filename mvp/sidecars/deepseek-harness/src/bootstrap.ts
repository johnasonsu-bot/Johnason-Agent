import { readFileSync } from "node:fs";


export const FIXED_PLUGINS = Object.freeze([
  "@deepseek-ai/dsh-agent",
  "@deepseek-ai/dsh-session-persistence-jsonl",
  "@deepseek-ai/dsh-session-checkpoint-policy",
  "@deepseek-ai/dsh-llm-deepseek",
  "@johnason/deepseek-harness-host-v2",
]);


function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}


export function loadFixedPreset(document) {
  if (!isRecord(document) || document.schema !== "workbench.runtime.dsh.fixed_preset.v1") {
    throw new Error("fixed DSH preset schema is invalid");
  }
  if (document.runtime_id !== "dsh") {
    throw new Error("fixed DSH preset runtime identity is invalid");
  }
  if (!Array.isArray(document.plugins)
      || document.plugins.length !== FIXED_PLUGINS.length
      || document.plugins.some((plugin, index) => plugin !== FIXED_PLUGINS[index])) {
    throw new Error("fixed DSH preset plugin set changed");
  }
  if (!isRecord(document.policy)
      || document.policy.plugin_download !== false
      || document.policy.user_plugin_scan !== false) {
    throw new Error("dynamic plugin loading and user plugin scanning are forbidden");
  }
  if (Object.keys(document.policy).sort().join(",") !== "plugin_download,user_plugin_scan") {
    throw new Error("fixed DSH preset policy has unknown fields");
  }
  return Object.freeze({
    schema: document.schema,
    runtime_id: document.runtime_id,
    plugins: FIXED_PLUGINS,
    policy: Object.freeze({ plugin_download: false, user_plugin_scan: false }),
  });
}


export function readFixedPreset(path) {
  return loadFixedPreset(JSON.parse(readFileSync(path, "utf8")));
}

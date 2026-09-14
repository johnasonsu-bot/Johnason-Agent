import { createRequire } from "node:module";
import { join, isAbsolute } from "node:path";
import { homedir } from "node:os";
import { DataPlatform, COMMANDS, safeError } from "./dataplatform.mjs";
export async function runDataPlatformCli(argv) {
  let corePath,
    command,
    dataRoot = join(homedir(), ".johnason-dsh"),
    input = {},
    projectId;
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (["--core", "--data-dir", "--input", "--project"].includes(flag)) {
      const value = argv[++i];
      if (value === undefined) throw Error("DP_INVALID_INPUT");
      if (flag === "--core") corePath = value;
      else if (flag === "--data-dir") dataRoot = value;
      else if (flag === "--input") {
        try {
          input = JSON.parse(value);
        } catch {
          throw Error("DP_INVALID_INPUT");
        }
      } else projectId = Number(value);
    } else if (!flag.startsWith("-") && !command) command = flag;
    else throw Error("DP_INVALID_INPUT");
  }
  if (
    !corePath ||
    !isAbsolute(corePath) ||
    !["login", "status", "logout", ...COMMANDS].includes(command)
  )
    throw Error(
      "Usage: dataplatform --core <absolute core path> [--data-dir <path>] <login|status|logout|command> [--project ID] [--input nonsecret-JSON]",
    );
  const require = createRequire(
    new URL(
      "../../../third_party/deepseek-harness/apps/cli/package.json",
      import.meta.url,
    ),
  );
  const { Context } = await import(require.resolve("@deepseek-ai/cordis"));
  const { default: Authorization } = await import(
    new URL(
      "../../../third_party/deepseek-harness/packages/credentials/authorization/lib/index.js",
      import.meta.url,
    )
  );
  const { default: Credentials, passwordPrompt } = await import(
    "./credentials-plugin.mjs"
  );
  const ctx = new Context();
  let service;
  try {
    await ctx.plugin(Credentials, {
      path: join(dataRoot, "vault.enc"),
      mode: "headless",
    });
    await ctx.plugin(Authorization);
    service = new DataPlatform({ corePath, credentials: ctx.credentials });
    ctx.authorization.registerFlow(service.flow);
    let result;
    if (command === "login")
      result = await ctx.authorization.begin({
        key: service.key,
        interaction: {
          notify() {},
          prompt: (p) => passwordPrompt(p.message + ": "),
        },
      });
    else if (command === "status") result = await service.status();
    else if (command === "logout") result = await service.logout();
    else {
      let password;
      try {
        if (["user.create", "user.update"].includes(command))
          password = await passwordPrompt(
            command === "user.create"
              ? "New user password: "
              : "New user password (empty keeps existing): ",
          );
        result = await service.executeHuman(
          { command, input, ...(projectId === undefined ? {} : { projectId }) },
          password || undefined,
        );
      } finally {
        password = undefined;
      }
    }
    process.stdout.write(JSON.stringify(result) + "\n");
  } catch (error) {
    process.stderr.write(JSON.stringify(safeError(error)) + "\n");
    process.exitCode = 1;
  } finally {
    await service?.close();
    await ctx.fiber.dispose();
  }
}

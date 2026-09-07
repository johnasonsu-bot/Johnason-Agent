import { _electron as electron, type ElectronApplication, type ElectronOptions } from "@playwright/test";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

type TestElectronOptions = Omit<ElectronOptions, "env"> & {
  env?: Record<string, string | undefined>;
  isolationDirectory?: string;
};

const unsafeEnvironmentName = /(?:^HERMES_|^WORKBENCH_|API[_-]?KEY|TOKEN|PASSWORD|CREDENTIAL|SECRET)/i;

export function isolatedElectronEnvironment(
  isolationDirectory: string,
  inherited: NodeJS.ProcessEnv = process.env,
  overrides: Record<string, string | undefined> = {},
): Record<string, string | undefined> {
  const environment = Object.fromEntries(
    Object.entries(inherited).filter(([name]) => !unsafeEnvironmentName.test(name)),
  );
  const deliberateOverrides = Object.fromEntries(Object.entries(overrides).filter(
    ([name, value]) => !unsafeEnvironmentName.test(name) || value !== inherited[name],
  ));
  return {
    ...environment,
    HERMES_PYTHON: path.resolve("../.venv/bin/python"),
    HERMES_RUNTIME_DIR: path.join(isolationDirectory, "runtime"),
    HERMES_LMSTUDIO_BASE_URL: "http://127.0.0.1:1",
    ...deliberateOverrides,
  };
}

export async function launchTestElectron(options: TestElectronOptions = {}): Promise<ElectronApplication> {
  const { isolationDirectory: requestedDirectory, env: overrides, args = [path.resolve(".")], ...launchOptions } = options;
  const isolationDirectory = requestedDirectory ?? await mkdtemp(path.join(os.tmpdir(), "hermes-electron-test-"));
  const userDataDirectory = path.join(isolationDirectory, "user-data");
  return electron.launch({
    ...launchOptions,
    args: [...args, `--user-data-dir=${userDataDirectory}`],
    env: isolatedElectronEnvironment(isolationDirectory, process.env, overrides),
  });
}

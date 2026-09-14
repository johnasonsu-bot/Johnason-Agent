import { createRequire } from "node:module";
import { isAbsolute } from "node:path";
import { realpathSync } from "node:fs";
import { createHash } from "node:crypto";
export const COMMANDS = Object.freeze([
  "project.list",
  "role.list",
  "role.create",
  "role.update",
  "user.list",
  "user.create",
  "user.update",
  "user.role.set",
  "permission.catalog",
  "permission.config.get",
  "permission.config.save",
  "permission.check",
]);
const preparedCores = new Map();
export const CORE_ENV_KEYS = [
  "DB_HOST",
  "DB_PORT",
  "DB_USER",
  "DB_PASSWORD",
  "DB_NAME",
  "DB_TIMEZONE",
  "JWT_SECRET",
  "JWT_EXPIRES_IN",
  "BCRYPT_SALT_ROUNDS",
];
export function prepareTrustedCore(corePath) {
  if (!isAbsolute(corePath)) throw fail("DP_CORE_REQUIRED");
  const path = realpathSync(corePath);
  if (!preparedCores.has(path))
    preparedCores.set(path, createRequire(import.meta.url)(path).createCore());
  return path;
}
/** Process owner only: plugin replacement must not close the shared database pool. */
export async function closeTrustedCores() {
  const cores = [...preparedCores.values()];
  preparedCores.clear();
  await Promise.all(cores.map((core) => core.close()));
}
const fail = (code) => Object.assign(new Error(code), { code });
const secretKey =
  /password|passwd|token|secret|authorization|credential|cookie|api.?key/i;
const knownErrors = new Set([
  "VAULT_LOCKED",
  "DP_UNKNOWN_COMMAND",
  "DP_SECRET_INPUT",
  "DP_CORE_MISMATCH",
  "DP_LOGIN_REQUIRED",
  "DP_INVALID_INPUT",
  "DP_CORE_REQUIRED",
  "DP_CANCELLED",
  "DP_UNAUTHORIZED",
  "DP_PERMISSION_DENIED",
  "DP_NOT_FOUND",
  "DP_VERSION_CONFLICT",
  "DP_UNAVAILABLE",
  "DP_REVOCATION_FAILED",
]);
const statusErrors = {
  400: "DP_INVALID_INPUT",
  401: "DP_UNAUTHORIZED",
  403: "DP_PERMISSION_DENIED",
  404: "DP_NOT_FOUND",
  409: "DP_VERSION_CONFLICT",
  503: "DP_UNAVAILABLE",
};
export function safeError(error) {
  const status = Number(error?.statusCode ?? error?.status);
  return {
    error: knownErrors.has(error?.code)
      ? error.code
      : (statusErrors[status] ?? "DP_OPERATION_FAILED"),
    ...(Object.hasOwn(statusErrors, status) ? { status } : {}),
  };
}
function sanitize(value, secrets = []) {
  // Match JSON transport semantics before crossing the native tool boundary.
  if (value === undefined) return null;
  if (value instanceof Date) return value.toJSON();
  if (typeof value === "string") {
    for (const secret of secrets)
      if (secret) value = value.split(secret).join("[redacted]");
    return value;
  }
  if (Array.isArray(value)) return value.map((v) => sanitize(v, secrets));
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .filter(([k, v]) => !secretKey.test(k) && v !== undefined)
        .map(([k, v]) => [k, sanitize(v, secrets)]),
    );
  return value;
}
function rejectSecrets(value) {
  if (value && typeof value === "object")
    for (const [k, v] of Object.entries(value)) {
      if (secretKey.test(k)) throw fail("DP_SECRET_INPUT");
      rejectSecrets(v);
    }
}
export class DataPlatform {
  #core;
  #credentials;
  #path;
  #ownsCore;
  #closed = false;
  constructor({ corePath, credentials, core }) {
    if (!corePath || !isAbsolute(corePath)) throw fail("DP_CORE_REQUIRED");
    this.#path = core ? corePath : realpathSync(corePath);
    this.key = `dataplatform/core-${createHash("sha256").update(this.#path).digest("hex")}`;
    this.#credentials = credentials;
    this.#ownsCore = Boolean(core) || !preparedCores.has(this.#path);
    this.#core =
      core ??
      preparedCores.get(this.#path) ??
      createRequire(import.meta.url)(this.#path).createCore();
    this.flow = {
      key: this.key,
      label: "Data Platform",
      methods: [{ id: "password", label: "平台账号登录" }],
      run: async (session) => {
        session.signal.throwIfAborted();
        // A locked Vault must fail before requesting the independent platform password.
        const previous = await this.#grant();
        const username = await session.prompt({
          kind: "text",
          message: "Data Platform username",
        });
        let password = await session.prompt({
            kind: "secret",
            message: "Data Platform password",
          }),
          token;
        try {
          session.signal.throwIfAborted();
          ({ token } = await this.#core.login({ username, password }));
          if (typeof token !== "string" || !token)
            throw fail("DP_OPERATION_FAILED");
          session.signal.throwIfAborted();
          if (previous && previous !== token) {
            try {
              await this.#core.logout(previous);
            } catch {
              throw fail("DP_REVOCATION_FAILED");
            }
          }
          session.signal.throwIfAborted();
          await this.#credentials.modifyRecord(this.key, () => ({
            kind: "grant",
            payload: { corePath: this.#path, token },
          }));
        } catch (cause) {
          if (token) await this.#core.logout(token).catch(() => {});
          const error = safeError(cause);
          throw Object.assign(
            fail(cause?.name === "AbortError" ? "DP_CANCELLED" : error.error),
            error.status ? { statusCode: error.status } : {},
          );
        } finally {
          password = "";
          token = undefined;
        }
      },
    };
  }
  async #grant() {
    const record = await this.#credentials.readRecord(this.key);
    if (!record) return null;
    if (record.kind !== "grant" || record.payload?.corePath !== this.#path)
      throw fail("DP_CORE_MISMATCH");
    if (typeof record.payload.token !== "string" || !record.payload.token)
      throw fail("DP_LOGIN_REQUIRED");
    return record.payload.token;
  }
  async #handleFailure(cause) {
    const error = safeError(cause);
    if (error.status === 401 || error.error === "DP_UNAUTHORIZED")
      await this.#credentials.deleteRecord(this.key);
    throw Object.assign(
      fail(error.error),
      error.status ? { statusCode: error.status } : {},
    );
  }
  async status() {
    const token = await this.#grant();
    if (!token) return { authorized: false };
    try {
      return {
        authorized: true,
        user: sanitize(await this.#core.profile(token), [token]),
      };
    } catch (cause) {
      return this.#handleFailure(cause);
    }
  }
  async logout() {
    const token = await this.#grant();
    let revoked = true;
    try {
      if (token) await this.#core.logout(token);
    } catch {
      revoked = false;
    } finally {
      await this.#credentials.deleteRecord(this.key);
    }
    return {
      authorized: false,
      revoked,
      ...(revoked ? {} : { error: "DP_REVOCATION_FAILED" }),
    };
  }
  async execute(args) {
    return this.#execute(args);
  }
  async executeHuman(args, password) {
    if (
      password !== undefined &&
      !["user.create", "user.update"].includes(args?.command)
    )
      throw fail("DP_SECRET_INPUT");
    return this.#execute(args, password);
  }
  async #execute(args, password) {
    if (
      !args ||
      typeof args !== "object" ||
      Array.isArray(args) ||
      Object.keys(args).some(
        (k) => !["command", "input", "projectId"].includes(k),
      )
    )
      throw fail("DP_INVALID_INPUT");
    if (!COMMANDS.includes(args.command)) throw fail("DP_UNKNOWN_COMMAND");
    rejectSecrets(args.input);
    if (
      args.input !== undefined &&
      (!args.input ||
        typeof args.input !== "object" ||
        Array.isArray(args.input))
    )
      throw fail("DP_INVALID_INPUT");
    if (
      args.projectId !== undefined &&
      (!Number.isSafeInteger(args.projectId) || args.projectId <= 0)
    )
      throw fail("DP_INVALID_INPUT");
    const token = await this.#grant();
    if (!token) throw fail("DP_LOGIN_REQUIRED");
    try {
      return sanitize(
        await this.#core.execute({
          token,
          command: args.command,
          projectId: args.projectId,
          input: {
            ...args.input,
            ...(password === undefined ? {} : { password }),
          },
        }),
        [token, password],
      );
    } catch (cause) {
      return this.#handleFailure(cause);
    }
  }
  async close() {
    if (!this.#closed && this.#ownsCore) await this.#core.close();
    this.#closed = true;
  }
}

import test from "node:test";
import assert from "node:assert/strict";
import { DataPlatform } from "../src/dataplatform.mjs";
function fixture() {
  let record,
    locked = false,
    closed = false;
  const calls = [];
  const credentials = {
    async readRecord() {
      if (locked) throw Object.assign(Error(), { code: "VAULT_LOCKED" });
      return record;
    },
    async modifyRecord(k, fn) {
      if (locked) throw Object.assign(Error(), { code: "VAULT_LOCKED" });
      record = await fn(record);
    },
    async deleteRecord() {
      record = undefined;
    },
  };
  const core = {
    async login() {
      return { token: "private-token", user: { id: 1, password: "hidden" } };
    },
    async profile(token) {
      calls.push(token);
      return { id: 1, debug: "private-token" };
    },
    async execute(args) {
      calls.push(args);
      return { ok: true, token: args.token, nested: { value: args.token } };
    },
    async logout(t) {
      calls.push(t);
    },
    async close() {
      closed = true;
    },
  };
  return {
    service: new DataPlatform({
      corePath: "/trusted/core.js",
      credentials,
      core,
    }),
    calls,
    lock() {
      locked = true;
    },
    get record() {
      return record;
    },
    get closed() {
      return closed;
    },
  };
}
test("flow secret prompts, encrypted grant, execution sanitization and logout lifecycle", async () => {
  const f = fixture(),
    prompts = [];
  await f.service.flow.run({
    signal: new AbortController().signal,
    prompt: async (p) => {
      prompts.push(p);
      return p.kind === "secret" ? "secret" : "alice";
    },
  });
  assert.equal(prompts[1].kind, "secret");
  assert.equal(f.record.kind, "grant");
  assert.equal(f.record.payload.corePath, "/trusted/core.js");
  assert.equal(
    JSON.stringify(
      await f.service.execute({ command: "project.list", input: {} }),
    ).includes("private-token"),
    false,
  );
  await f.service.logout();
  assert.equal(f.record, undefined);
  await f.service.close();
  assert.equal(f.closed, true);
});
test("locked vault, cancelled flow, unknown commands and credential inputs fail closed", async () => {
  const f = fixture();
  f.lock();
  await assert.rejects(f.service.status(), { code: "VAULT_LOCKED" });
  await assert.rejects(f.service.execute({ command: "oops" }), {
    code: "DP_UNKNOWN_COMMAND",
  });
  await assert.rejects(
    f.service.execute({ command: "user.create", input: { password: "bad" } }),
    { code: "DP_SECRET_INPUT" },
  );
  const c = new AbortController();
  c.abort();
  await assert.rejects(
    f.service.flow.run({
      signal: c.signal,
      prompt: async () => {
        throw Error("must not prompt");
      },
    }),
  );
});
test("grant bound to trusted core", async () => {
  const f = fixture();
  await f.service.flow.run({
    signal: new AbortController().signal,
    prompt: async () => "answer",
  });
  f.record.payload.corePath = "/other/core.js";
  await assert.rejects(f.service.status(), { code: "DP_CORE_MISMATCH" });
});
import { createRequire } from "node:module";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import Provider from "../src/credentials-plugin.mjs";
import { createDataPlatformHandler } from "../src/dataplatform-ui.mjs";
import { resolveLaunch } from "../src/launch-config.mjs";
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
test("real native authorization commits to encrypted Vault and cancels without a grant", async () => {
  const ctx = new Context();
  const path = join(await mkdtemp(join(tmpdir(), "dp-native-")), "vault.enc");
  try {
    const provider = new Provider(ctx, { path, mode: "web" });
    await provider.vault.initialize("test-master");
    await ctx.plugin(Authorization);
    const f = fixture();
    const service = new DataPlatform({
      corePath: "/trusted/core.js",
      credentials: ctx.credentials,
      core: {
        login: async () => ({ token: "private-session", user: { id: 1 } }),
        profile: async () => ({ id: 1 }),
        logout: async () => {},
        close: async () => {},
      },
    });
    ctx.authorization.registerFlow(service.flow);
    const result = await ctx.authorization.begin({
      key: service.key,
      interaction: { notify() {}, prompt: async () => "answer" },
    });
    assert.deepEqual(result, { status: "authorized" });
    assert.equal(
      (await readFile(path, "utf8")).includes("private-session"),
      false,
    );
    assert.deepEqual(await service.status(), {
      authorized: true,
      user: { id: 1 },
    });
    await service.logout();
    const c = new AbortController();
    c.abort();
    assert.deepEqual(
      await ctx.authorization.begin({
        key: service.key,
        signal: c.signal,
        interaction: { notify() {}, prompt: async () => "answer" },
      }),
      { status: "cancelled" },
    );
    assert.deepEqual(await service.status(), { authorized: false });
    provider.vault.lock();
  } finally {
    await ctx.fiber.dispose();
  }
});
test("same-origin UI runs native flow and structured command while rejecting hostile origins", async () => {
  const f = fixture();
  const auth = {
    async begin({ interaction, signal }) {
      await f.service.flow.run({ ...interaction, signal });
      return { status: "authorized" };
    },
  };
  const server = createServer(createDataPlatformHandler(f.service, auth));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const post = (path, data, hostOrigin = origin) =>
      fetch(origin + path, {
        method: "POST",
        headers: { origin: hostOrigin, "content-type": "application/json" },
        body: JSON.stringify(data),
      });
    assert.equal(
      (
        await post(
          "/dataplatform/login",
          { username: "alice", password: "secret" },
          "https://evil.example",
        )
      ).status,
      403,
    );
    assert.equal(
      (
        await post("/dataplatform/login", {
          username: "alice",
          password: "secret",
        })
      ).status,
      200,
    );
    const result = await (
      await post("/dataplatform/execute", {
        command: "project.list",
        input: {},
      })
    ).text();
    assert.match(result, /true/);
    assert.doesNotMatch(result, /private-token/);
    const html = await (await fetch(origin + "/dataplatform")).text();
    assert.match(html, /type="password"/);
    assert.doesNotMatch(html, /localStorage|innerHTML/);
    assert.equal(
      (
        await post("/dataplatform/execute", {
          command: "project.list",
          corePath: "/evil",
        })
      ).status,
      400,
    );
  } finally {
    await new Promise((r) => server.close(r));
  }
});
test("CommonJS core is loaded from host absolute path and invoked", async () => {
  const dir = await mkdtemp(join(tmpdir(), "dp-cjs-")),
    corePath = join(dir, "core.cjs");
  await writeFile(
    corePath,
    "exports.createCore=()=>({login:async()=>({token:'private-token'}),profile:async()=>({id:7}),execute:async({command})=>({executed:command}),logout:async()=>{},close:async()=>{}})",
  );
  let record;
  const service = new DataPlatform({
    corePath,
    credentials: {
      readRecord: async () => record,
      modifyRecord: async (k, fn) => {
        record = await fn(record);
      },
      deleteRecord: async () => {
        record = undefined;
      },
    },
  });
  try {
    await service.flow.run({
      signal: new AbortController().signal,
      prompt: async () => "answer",
    });
    assert.deepEqual(await service.execute({ command: "project.list" }), {
      executed: "project.list",
    });
  } finally {
    await service.close();
  }
});
test("DB env is forwarded only with explicitly selected host core", () => {
  const options = {
    homeDir: "/tmp",
    repoRoot: "/repo",
    env: {
      DB_PASSWORD: "sensitive",
      JWT_SECRET: "signing",
      UNRELATED_SECRET: "hidden",
    },
    nodeVersion: "22.20.0",
  };
  assert.equal(resolveLaunch(["web"], options).env.DB_PASSWORD, undefined);
  const launch = resolveLaunch(["web", "--core", "/trusted/core.js"], options);
  assert.equal(launch.env.DB_PASSWORD, "sensitive");
  assert.equal(launch.env.UNRELATED_SECRET, undefined);
  assert.equal(launch.corePath, "/trusted/core.js");
});
test("human user provisioning password reaches core only and is scrubbed from result", async () => {
  const f = fixture();
  await f.service.flow.run({
    signal: new AbortController().signal,
    prompt: async () => "answer",
  });
  await f.service.executeHuman(
    { command: "user.create", input: { username: "bob" } },
    "human-secret",
  );
  assert.equal(f.calls.at(-1).input.password, "human-secret");
  await assert.rejects(
    f.service.executeHuman({ command: "project.list" }, "secret"),
    { code: "DP_SECRET_INPUT" },
  );
});
import { PassThrough } from "node:stream";
import { runDataPlatformCli } from "../src/dataplatform-cli.mjs";
test("terminal CLI unlocks native Vault, signs in, executes, and never echoes secrets", async () => {
  const dir = await mkdtemp(join(tmpdir(), "dp-terminal-")),
    corePath = join(dir, "core.cjs");
  await writeFile(
    corePath,
    "exports.createCore=()=>({login:async({password})=>{if(password!=='platform-secret')throw Error('bad');return {token:'session-secret'}},profile:async()=>({id:7}),execute:async({token,command})=>({executed:command,token}),logout:async()=>{},close:async()=>{}})",
  );
  const stdin = new PassThrough(),
    stderr = new PassThrough(),
    stdout = new PassThrough();
  stdin.isTTY = stderr.isTTY = true;
  stdin.isRaw = false;
  stdin.setRawMode = (v) => {
    stdin.isRaw = v;
  };
  let printed = "";
  const descriptors = Object.fromEntries(
    ["stdin", "stderr", "stdout"].map((k) => [
      k,
      Object.getOwnPropertyDescriptor(process, k),
    ]),
  );
  for (const [k, value] of Object.entries({ stdin, stderr, stdout }))
    Object.defineProperty(process, k, { configurable: true, value });
  const answers = [
    "vault-secret",
    "vault-secret",
    "alice",
    "platform-secret",
    "vault-secret",
  ];
  stderr.on("data", (chunk) => {
    printed += chunk;
    if (String(chunk).endsWith(": "))
      setImmediate(() => stdin.write(answers.shift() + "\r"));
  });
  stdout.on("data", (chunk) => {
    printed += chunk;
  });
  try {
    await runDataPlatformCli(["--core", corePath, "--data-dir", dir, "login"]);
    await runDataPlatformCli([
      "--core",
      corePath,
      "--data-dir",
      dir,
      "project.list",
    ]);
  } finally {
    for (const [k, d] of Object.entries(descriptors))
      Object.defineProperty(process, k, d);
    stdin.destroy();
    stderr.destroy();
    stdout.destroy();
  }
  assert.match(printed, /authorized/);
  assert.match(printed, /project.list/);
  assert.doesNotMatch(printed, /vault-secret|platform-secret|session-secret/);
});
import {
  prepareTrustedCore,
  closeTrustedCores,
  safeError,
} from "../src/dataplatform.mjs";
test("prepared core survives plugin dispose and reapply after runtime env removal", async () => {
  const dir = await mkdtemp(join(tmpdir(), "dp-reapply-")),
    corePath = join(dir, "core.cjs");
  await writeFile(
    corePath,
    `let created=0,closed=0;exports.createCore=()=>{if(!process.env.JWT_SECRET)throw Error('JWT required');created++;return {profile:async()=>({created,closed}),close:async()=>{closed++}}}`,
  );
  const previous = process.env.JWT_SECRET;
  process.env.JWT_SECRET = "synthetic";
  try {
    const canonicalPath = prepareTrustedCore(corePath);
    delete process.env.JWT_SECRET;
    const credentials = {
      readRecord: async () => ({
        kind: "grant",
        payload: { corePath: canonicalPath, token: "private" },
      }),
    };
    const first = new DataPlatform({ corePath, credentials });
    await first.close();
    const second = new DataPlatform({ corePath, credentials });
    assert.deepEqual(await second.status(), {
      authorized: true,
      user: { created: 1, closed: 0 },
    });
    await second.close();
    await closeTrustedCores();
  } finally {
    if (previous === undefined) delete process.env.JWT_SECRET;
    else process.env.JWT_SECRET = previous;
  }
});
test("unauthorized profile and execute clear stale grants and preserve safe status codes", async () => {
  for (const operation of ["status", "execute"]) {
    let record = {
      kind: "grant",
      payload: { corePath: "/trusted/core.js", token: "old" },
    };
    const reject = async () => {
      throw Object.assign(Error("private database details"), {
        statusCode: 401,
      });
    };
    const service = new DataPlatform({
      corePath: "/trusted/core.js",
      credentials: {
        readRecord: async () => record,
        deleteRecord: async () => {
          record = undefined;
        },
      },
      core: { profile: reject, execute: reject, close: async () => {} },
    });
    await assert.rejects(
      operation === "status"
        ? service.status()
        : service.execute({ command: "project.list" }),
      { code: "DP_UNAUTHORIZED", statusCode: 401 },
    );
    assert.equal(record, undefined);
  }
  assert.deepEqual(
    safeError(Object.assign(Error("private-db-password"), { statusCode: 403 })),
    { error: "DP_PERMISSION_DENIED", status: 403 },
  );
});
test("logout clears local grant on revocation failure and reports fixed outcome", async () => {
  let record = {
    kind: "grant",
    payload: { corePath: "/trusted/core.js", token: "old" },
  };
  const service = new DataPlatform({
    corePath: "/trusted/core.js",
    credentials: {
      readRecord: async () => record,
      deleteRecord: async () => {
        record = undefined;
      },
    },
    core: {
      logout: async () => {
        throw Error("sensitive");
      },
      close: async () => {},
    },
  });
  assert.deepEqual(await service.logout(), {
    authorized: false,
    revoked: false,
    error: "DP_REVOCATION_FAILED",
  });
  assert.equal(record, undefined);
});
test("relogin revokes previous session before replacing encrypted grant", async () => {
  let record = {
    kind: "grant",
    payload: { corePath: "/trusted/core.js", token: "old" },
  };
  const revoked = [];
  const service = new DataPlatform({
    corePath: "/trusted/core.js",
    credentials: {
      readRecord: async () => record,
      modifyRecord: async (k, fn) => {
        assert.deepEqual(revoked, ["old"]);
        record = await fn(record);
      },
    },
    core: {
      login: async () => ({ token: "new" }),
      logout: async (token) => revoked.push(token),
      close: async () => {},
    },
  });
  await service.flow.run({
    signal: new AbortController().signal,
    prompt: async () => "answer",
  });
  assert.equal(record.payload.token, "new");
});

test('platform outputs omit undefined fields and preserve dates as ISO JSON', async () => {
 const date = new Date('2026-09-14T00:00:00.000Z');
 const service = new DataPlatform({corePath:'/trusted/core.js',credentials:{async readRecord(){return {kind:'grant',payload:{corePath:'/trusted/core.js',token:'fixture-token'}};}},core:{async execute(){return [{id:1,permissions:{mode:undefined,actions:undefined,modules:['overview']},createdAt:date,nested:[undefined,null],password:'hidden'}];}}});
 const result=await service.execute({command:'role.list'});
 assert.deepEqual(result,[{id:1,permissions:{modules:['overview']},createdAt:date.toISOString(),nested:[null,null]}]);
 assert.deepEqual(result,JSON.parse(JSON.stringify(result)));
});

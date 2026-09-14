import { readFile } from "node:fs/promises";
import { safeError, COMMANDS } from "./dataplatform.mjs";
const paths = [
  "/dataplatform",
  "/dataplatform/status",
  "/dataplatform/login",
  "/dataplatform/logout",
  "/dataplatform/execute",
];
export function createDataPlatformHandler(service, authorization) {
  return async (req, res) => {
    const send = (status, value, type = "application/json") => {
      res.writeHead(status, {
        "content-type": type,
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "content-security-policy":
          "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'",
      });
      res.end(type === "application/json" ? JSON.stringify(value) : value);
    };
    const host = req.headers.host,
      origin = `http://${host}`,
      port = req.socket.localPort;
    if (
      !["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(
        req.socket.remoteAddress,
      ) ||
      ![`localhost:${port}`, `127.0.0.1:${port}`, `[::1]:${port}`].includes(
        host,
      ) ||
      (req.headers.origin && req.headers.origin !== origin) ||
      (req.headers["sec-fetch-site"] &&
        !["same-origin", "none"].includes(req.headers["sec-fetch-site"]))
    )
      return send(403, { error: "Same-origin local requests required" });
    try {
      const path = new URL(req.url, origin).pathname;
      if (req.method === "GET" && path === "/dataplatform")
        return send(
          200,
          await readFile(
            new URL("../public/dataplatform.html", import.meta.url),
            "utf8",
          ),
          "text/html; charset=utf-8",
        );
      if (req.method === "GET" && path === "/dataplatform/status") {
        try {
          return send(200, { ...(await service.status()), commands: COMMANDS });
        } catch (cause) {
          return send(400, { ...safeError(cause), commands: COMMANDS });
        }
      }
      if (req.method !== "POST" || !paths.slice(2).includes(path))
        return send(405, { error: "Method not allowed" });
      if (
        req.headers.origin !== origin ||
        req.headers["content-type"]?.split(";")[0] !== "application/json"
      )
        return send(403, { error: "Same-origin JSON required" });
      let size = 0;
      const chunks = [];
      for await (const c of req) {
        size += c.length;
        if (size > 262144) return send(413, { error: "Request too large" });
        chunks.push(c);
      }
      const data = JSON.parse(Buffer.concat(chunks).toString());
      chunks.length = 0;
      if (path === "/dataplatform/login") {
        // The URL identifies this local integration; it is never an HTTP login target.
        // Older callers omit it and continue using the explicitly loaded shared core.
        if (data.url !== undefined) {
          let target;
          try { target = new URL(data.url); } catch {}
          if (!target || target.protocol !== "http:" ||
              !["127.0.0.1", "localhost"].includes(target.hostname) ||
              target.port !== "46120" || target.pathname !== "/" ||
              target.username || target.password || target.search || target.hash) {
            data.password = "";
            return send(400, { error: "DP_TARGET_NOT_CONFIGURED" });
          }
        }
        if (
          typeof data.username !== "string" ||
          typeof data.password !== "string" ||
          !data.password ||
          data.password.length > 4096
        )
          return send(400, { error: "Username and password required" });
        const controller = new AbortController();
        res.on("close", () => {
          if (!res.writableEnded) controller.abort();
        });
        try {
          const outcome = await authorization.begin({
            key: service.key,
            signal: controller.signal,
            interaction: {
              notify() {},
              async prompt(p) {
                return p.kind === "secret" ? data.password : data.username;
              },
            },
          });
          return send(200, outcome);
        } finally {
          data.password = "";
        }
      }
      return send(
        200,
        path === "/dataplatform/logout"
          ? await service.logout()
          : await (async () => {
              const { userPassword, ...args } = data;
              try {
                return await service.executeHuman(
                  args,
                  userPassword || undefined,
                );
              } finally {
                data.userPassword = "";
              }
            })(),
      );
    } catch (cause) {
      return send(400, safeError(cause));
    }
  };
}
export function installDataPlatformUi(ctx, service, authorization) {
  const handler = createDataPlatformHandler(service, authorization);
  for (const path of paths)
    ctx.effect(() => ctx.webServer.register({ kind: "exact", path, handler }));
  ctx.effect(() =>
    ctx.webServer.tapIndex((html) =>
      html.replace(
        "</body>",
        '<a href="/dataplatform" target="_blank" rel="noopener" style="position:fixed;bottom:44px;right:12px;z-index:99999;background:white;padding:6px">Data Platform</a></body>',
      ),
    ),
  );
}

#!/usr/bin/env node
import { readFixedPreset } from "./bootstrap.mjs";
import { readPreopenedGrantChannel } from "./grant-channel.mjs";
import { createSidecar, serveNdjson } from "./server.mjs";

readFixedPreset(new URL("../cordis.host-v2.yml", import.meta.url));
if (process.argv.length !== 2) throw new Error("DSH Host v2 rejects argv configuration");
const instanceDigest = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const sidecar = createSidecar({
  grantChannel: readPreopenedGrantChannel(3, instanceDigest),
  runtimeId: "dsh",
  buildId: "dsh:fixed-host-v2-smoke",
  instanceDigest,
});
serveNdjson(sidecar);

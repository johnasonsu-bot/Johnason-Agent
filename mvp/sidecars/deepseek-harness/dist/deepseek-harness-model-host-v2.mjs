#!/usr/bin/env node
import { readFixedPreset } from "./bootstrap.mjs";
import { startPreopenedGrantReceiver } from "./grant-channel.mjs";
import { createSidecar, serveNdjson } from "./server.mjs";

readFixedPreset(new URL("../cordis.host-v2.yml", import.meta.url));
if (process.argv.length !== 2) throw new Error("DSH Host v2 rejects argv configuration");
const runtimeId = "dsh";
const buildId = "dsh:model-host-v2-r1";
const descriptor = process.env.WORKBENCH_PROVIDER_GRANT_FD ?? "";
delete process.env.WORKBENCH_PROVIDER_GRANT_FD;
if (!/^[0-9]+$/.test(descriptor)) throw new Error("DSH Provider Grant descriptor is missing");
const receiver = startPreopenedGrantReceiver(Number(descriptor), { runtimeId, buildId });
const sidecar = createSidecar({
  grantChannel: receiver.channel,
  runtimeId,
  buildId,
});
serveNdjson(sidecar, process.stdin, process.stdout, receiver.ready);

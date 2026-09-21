import { DataPlatform, COMMANDS } from "./dataplatform.mjs";
import { installDataPlatformUi } from "./dataplatform-ui.mjs";
export const name = "johnason-dataplatform";
export const inject = ["authorization", "credentials", "tools"];
export function apply(ctx, config) {
  const service = new DataPlatform({
    corePath: config.corePath,
    credentials: ctx.credentials,
  });
  ctx.effect(() => ctx.authorization.registerFlow(service.flow));
  ctx.effect(() => () => service.close());
  ctx.tools.register({
    name: "dataplatform_execute",
    description:
      "Execute an authorized Data Platform command. Sign in using the local Data Platform page first. Never supply passwords or tokens.",
    parameters: {
      type: "object",
      properties: {
        command: { type: "string", enum: COMMANDS },
        projectId: { type: "integer", minimum: 1 },
        input: { type: "object" },
      },
      required: ["command"],
      additionalProperties: false,
    },
    output: {
      schema: {},
      render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
    },
    execute: (args) => service.execute(args),
  });
  ctx.inject(["webServer"], (ui) =>
    installDataPlatformUi(ui, service, ctx.authorization),
  );
}

import { expect, test } from "@playwright/test";
import { ApiRequestError, isRetryableConversationError } from "../src/renderer/api";
import { describeConversationError, reduceConversationStatus } from "../src/renderer/conversations/ConversationWorkspace";
import { runWithConversationRetry } from "../src/renderer/conversations/retry";

test("retries a retryable conversation turn with the same operation", async () => {
  let attempts = 0;
  const result = await runWithConversationRetry(
    async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("agent turn is retryable");
      return "completed";
    },
    (error) => error instanceof Error && error.message.includes("agent turn is retryable"),
    { maxAttempts: 2, delayMs: 0 },
  );

  expect(result).toBe("completed");
  expect(attempts).toBe(2);
});

test("does not retry a local inference read timeout", () => {
  const error = new ApiRequestError(
    "本地服务请求失败（503）：agent turn is retryable: ReadTimeout",
    503,
  );

  expect(isRetryableConversationError(error)).toBe(false);
});

test("describes local inference timeout without a workspace fixture diagnosis", () => {
  expect(describeConversationError(new ApiRequestError(
    "本地服务请求失败（503）：agent turn is retryable: ReadTimeout",
    503,
  ))).toContain("模型请求超时");
});

for (const terminal of ["completed", "failed", "reconciliation_required", "paused"] as const) {
  test(`${terminal} projection ignores a late queue repair`, () => {
    const current = reduceConversationStatus(undefined, terminal === "completed"
      ? { name: "turn_finished", value: { status: "completed", command_id: "turn-1" } }
      : terminal === "paused"
        ? { name: "conversation.status", value: { status: "paused" } }
        : { name: "turn_failed", value: { response_status: terminal, command_id: "turn-1" } });

    expect(reduceConversationStatus(current, {
      name: "turn_queued",
      value: { command_id: "turn-1", status: "queued" },
    })).toEqual(current);
  });
}

test("paused projection accepts an explicit resume but ignores stale turn running", () => {
  const paused = reduceConversationStatus(undefined, {
    name: "conversation.status",
    value: { status: "paused" },
  });
  expect(reduceConversationStatus(paused, {
    name: "conversation.status",
    value: { status: "running", command_id: "turn-1" },
  })).toEqual(paused);
  expect(reduceConversationStatus(paused, {
    name: "conversation.status",
    value: { status: "running" },
  }).phase).toBe("running");
});

test("a terminal command accepts the queued lifecycle of a newer command", () => {
  const completed = reduceConversationStatus(undefined, {
    name: "turn_finished",
    value: { status: "completed", command_id: "turn-1" },
  });
  const queued = reduceConversationStatus(completed, {
    name: "turn_queued",
    value: { status: "queued", command_id: "turn-2" },
  });
  expect(queued.phase).toBe("queued");
  expect(queued.commandId).toBe("turn-2");
});

import { expect, test } from "@playwright/test";
import { ApiRequestError, isRetryableConversationError } from "../src/renderer/api";
import { describeConversationError } from "../src/renderer/conversations/ConversationWorkspace";
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

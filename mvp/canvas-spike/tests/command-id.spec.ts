import { expect, test } from "@playwright/test";
import { createConversationCommandId } from "../src/renderer/conversations/commandIds";

test("creates distinct command identities for consecutive turns in a persisted session", () => {
  const first = createConversationCommandId("message", "ui-session-0");
  const second = createConversationCommandId("message", "ui-session-0");

  expect(first).not.toBe(second);
  expect(first).toMatch(/^message-ui-session-0-/);
  expect(second).toMatch(/^message-ui-session-0-/);
});

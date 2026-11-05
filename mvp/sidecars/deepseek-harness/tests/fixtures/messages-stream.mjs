/** Anthropic-compatible Messages SSE emitted by the pinned DeepSeek adapter. */
export function messagesStream(events) {
  return events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join("");
}

function start({ model = "deepseek-native-tools", inputTokens = 6 } = {}) {
  return {
    type: "message_start",
    message: {
      id: "msg_fixture",
      type: "message",
      role: "assistant",
      model,
      content: [],
      usage: { input_tokens: inputTokens, output_tokens: 0 },
    },
  };
}

function end(stopReason, { outputTokens = 2 } = {}) {
  return [
    { type: "message_delta", delta: { stop_reason: stopReason }, usage: { output_tokens: outputTokens } },
    { type: "message_stop" },
  ];
}

/** Pass an array to preserve the exact text-delta boundaries under test. */
export function textStream(textOrChunks, options = {}) {
  const chunks = Array.isArray(textOrChunks) ? textOrChunks : [textOrChunks];
  return messagesStream([
    start(options),
    { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
    ...chunks.map(text => ({
      type: "content_block_delta", index: 0, delta: { type: "text_delta", text },
    })),
    { type: "content_block_stop", index: 0 },
    ...end("end_turn", options),
  ]);
}

export function toolCallStream(callId, name, args, options = {}) {
  return messagesStream([
    start(options),
    { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: callId, name, input: {} } },
    { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: JSON.stringify(args) } },
    { type: "content_block_stop", index: 0 },
    ...end("tool_use", options),
  ]);
}

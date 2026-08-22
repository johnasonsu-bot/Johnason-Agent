export function createConversationCommandId(kind: string, sessionId: string): string {
  const nonce = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${kind}-${sessionId}-${nonce}`;
}

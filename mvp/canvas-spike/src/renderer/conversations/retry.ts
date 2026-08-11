export type ConversationRetryOptions = {
  maxAttempts?: number;
  delayMs?: number;
};

export async function runWithConversationRetry<T>(
  operation: () => Promise<T>,
  shouldRetry: (error: unknown) => boolean,
  { maxAttempts = 2, delayMs = 500 }: ConversationRetryOptions = {},
): Promise<T> {
  const attempts = Math.max(1, Math.floor(maxAttempts));
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      if (attempt >= attempts || !shouldRetry(error)) throw error;
      if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  throw new Error("conversation retry exhausted");
}

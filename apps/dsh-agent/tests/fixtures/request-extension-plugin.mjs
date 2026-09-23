// Test-only route: exercises the official adapter and the live profile's extension registry.
// Its endpoint is confined to loopback, and its synthetic API key exists only in memory.
import { randomUUID } from 'node:crypto';
import { DeepSeekAdapter, resolveAdapterOptions } from '../../../../third_party/deepseek-harness/packages/llm/llm-deepseek/lib/index.js';
import { BlockAssembler, createUserMessage, LlmError } from '../../../../third_party/deepseek-harness/packages/llm/llm/lib/index.js';

export const name = 'request-extension-acceptance';
export const inject = ['webServer', 'deepseekLlmApiExtensions', 'credentials'];

export function apply(ctx, config) {
  const endpoint = new URL(config.baseURL);
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port
      || endpoint.username || endpoint.password) throw new Error('Acceptance transport must use loopback HTTP');
  const apiKey = randomUUID();
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact', path: '/request-extension-acceptance',
    handler: async (_request, response) => {
      try {
        let fields;
        const adapter = new DeepSeekAdapter({
          options: () => resolveAdapterOptions({ protocol: config.protocol ?? 'chat-completions', baseURL: endpoint.origin }),
          resolveApiKey: async () => {
            if (!config.useVault) return apiKey;
            const hit = await ctx.credentials.resolve('DEEPSEEK_API_KEY');
            if (!hit) throw new LlmError('No test model credential configured', 'MISSING_CREDENTIAL');
            return hit.value;
          },
          resolveUserId: () => '00000000-0000-4000-8000-000000000001',
          prepareExtensions: async request => {
            const prepared = await ctx.deepseekLlmApiExtensions.prepare(request);
            fields = prepared.fields;
            return prepared;
          },
        });
        const assembler = new BlockAssembler();
        for await (const chunk of adapter.stream({
          provider: 'deepseek-official', model: 'deepseek-chat', signal: AbortSignal.timeout(10000),
          messages: [createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Local request-extension acceptance' }] })],
        })) assembler.push(chunk);
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ fields, blocks: assembler.blocks(), finish: assembler.finish }));
      } catch (error) {
        response.writeHead(503, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: error.message, code: error.code, cause: error.cause?.message }));
      }
    },
  }));
}

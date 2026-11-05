// Test-only trusted plugin: fixed tool call, no user-controlled execution API.
export const name = 'dsh-acceptance-profile-plugin';
export const inject = ['webServer', 'tools'];
export function apply(ctx) {
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact', path: '/acceptance-fixture',
    handler: async (_req, res) => {
      try {
        const batch = await ctx.tools.execute({ signal: new AbortController().signal, calls: [{ type: 'tool-call', id: 'acceptance-marker', name: 'mcp__acceptance__marker', arguments: '{}' }] });
        const result = batch.results[0].result;
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ source: name, result }));
      } catch (error) {
        res.writeHead(503); res.end(JSON.stringify({ error: String(error) }));
      }
    },
  }));
}

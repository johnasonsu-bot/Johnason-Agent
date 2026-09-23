// Read-only local feature inventory; credentials and session policy stay in their host services.
export function apply(ctx, config = {}) {
  const providers = (Array.isArray(config.providers) ? config.providers : [])
    .filter(provider => provider && ['id', 'label', 'type'].every(key => typeof provider[key] === 'string' && provider[key].length > 0 && provider[key].length <= 256))
    .map(({ id, label, type }) => ({ id, label, type }));
  const metadata = {
    schemaVersion: 1,
    pluginId: '@johnason/dsh-memory-ui',
    features: ['vault', 'three-layer-memory', 'memory-recovery', 'semantic-subgraph', 'task-sandbox', 'systems', 'unified-authorization'],
    providers,
  };
  ctx.inject(['webServer'], web => {
    web.effect(() => web.webServer.register({ kind: 'exact', path: '/johnason/plugins', handler(req, res) {
      const send = (status, body) => {
        res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer' });
        res.end(JSON.stringify(body));
      };
      const host = req.headers.host;
      const port = req.socket.localPort;
      if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(req.socket.remoteAddress)
        || ![`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`].includes(host)
        || (req.headers.origin && req.headers.origin !== `http://${host}`)
        || (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) return send(403, { code: 'LOCAL_SAME_ORIGIN_REQUIRED' });
      if (req.method !== 'GET') return send(405, { code: 'METHOD_NOT_ALLOWED' });
      try {
        if (new URL(req.url, `http://${host}`).pathname !== '/johnason/plugins') return send(404, { code: 'ROUTE_NOT_FOUND' });
      } catch { return send(400, { code: 'INVALID_REQUEST' }); }
      return send(200, metadata);
    } }));
  });
}

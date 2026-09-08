import { readFile } from 'node:fs/promises';

const page = new URL('../public/vault.html', import.meta.url);
const paths = ['/vault', '/vault/status', '/vault/initialize', '/vault/unlock', '/vault/lock'];
const local = address => ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address);
const errorMessages = Object.freeze({
  VAULT_UNLOCK_FAILED: '保险箱认证失败：密码可能不正确，或文件完整性可能受损。不会自动删除或重置保险箱。',
  VAULT_DECRYPT_FAILED: '保险箱数据认证失败，文件可能已变化或受损。请锁定后重新解锁；不会自动删除或重置保险箱。',
  VAULT_FORMAT_ERROR: '保险箱格式无效或不受支持。请检查文件或使用可靠备份恢复；修改密码无法修复格式问题。不会自动删除或重置保险箱。',
  VAULT_BUSY: '保险箱正在被其他操作使用。请等待该操作完成后重试。',
  VAULT_LOCKED: '保险箱已锁定，请先解锁再继续。',
  VAULT_EXISTS: '保险箱已经初始化，请解锁已有保险箱；本次操作未覆盖它。',
  VAULT_NOT_INITIALIZED: '保险箱尚未初始化，请先创建保险箱再解锁。',
  VAULT_LOCK_OWNERSHIP_LOST: '保险箱操作锁的归属已变化。请停止并发操作后重试，不要删除锁或保险箱文件。',
  VAULT_OPERATION_FAILED: '保险箱操作失败，请重试或检查本地保险箱状态。不会自动删除或重置保险箱。',
});

/** HTTP handler restricted to local same-origin requests; responses never include input or credentials. */
export function createVaultHandler(vault) {
  return async (req, res) => {
    const send = (status, data, type = 'application/json') => {
      res.writeHead(status, { 'content-type': type, 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer', 'content-security-policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'" });
      res.end(type === 'application/json' ? JSON.stringify(data) : data);
    };
    const authority = req.headers.host;
    const port = req.socket.localPort;
    const authorities = [`127.0.0.1:${port}`, `localhost:${port}`, `[::1]:${port}`];
    if (!local(req.socket.remoteAddress) || !authorities.includes(authority)
      || (req.headers.origin && req.headers.origin !== `http://${authority}`)
      || (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site']))) return send(403, { error: 'Local same-origin requests only' });
    const path = new URL(req.url, `http://${authority}`).pathname;
    try {
      if (req.method === 'GET' && path === '/vault') return send(200, await readFile(page, 'utf8'), 'text/html; charset=utf-8');
      if (req.method === 'GET' && path === '/vault/status') return send(200, await vault.status());
      if (req.method !== 'POST' || !paths.slice(2).includes(path)) return send(405, { error: 'Method not allowed' });
      if (req.headers.origin !== `http://${authority}` || req.headers['content-type']?.split(';')[0] !== 'application/json') return send(403, { error: 'Same-origin JSON required' });
      let size = 0;
      const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 16384) return send(413, { error: 'Request too large' });
        chunks.push(chunk);
      }
      const data = JSON.parse(Buffer.concat(chunks, size).toString('utf8'));
      chunks.length = 0;
      if (path === '/vault/lock') vault.lock();
      else {
        if (typeof data.password !== 'string' || !data.password.length || data.password.length > 4096) return send(400, { error: 'Password required (maximum 4096 characters)' });
        try {
          if (path === '/vault/initialize') {
            if (data.password !== data.confirmation) return send(400, { error: 'Passwords do not match' });
            await vault.initialize(data.password);
          } else await vault.unlock(data.password);
        } finally { data.password = ''; data.confirmation = ''; }
      }
      return send(200, await vault.status());
    } catch (error) {
      const code = typeof error?.code === 'string' && Object.hasOwn(errorMessages, error.code)
        ? error.code : 'VAULT_OPERATION_FAILED';
      return send(400, { code, error: errorMessages[code] });
    }
  };
}

/** Add a same-origin page and a small link without replacing the native React application. */
export function installVaultUi(ctx, vault) {
  const handler = createVaultHandler(vault);
  for (const path of paths) ctx.effect(() => ctx.webServer.register({ kind: 'exact', path, handler }));
  ctx.effect(() => ctx.webServer.tapIndex(html => html.replace('</body>', '<a href="/vault" target="_blank" rel="noopener" style="position:fixed;bottom:8px;right:12px;z-index:99999;font:12px sans-serif;background:#fff;color:#17304a;padding:6px 10px;border:1px solid #ccc;border-radius:6px">Vault 解锁 / 锁定</a></body>')));
}

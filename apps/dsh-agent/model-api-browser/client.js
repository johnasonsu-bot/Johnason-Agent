/* Browser-only native module-loader factory. Secrets are sent only to the credentials Remote. */
window.__ModuleLoader__.load({
  id: '@johnason/dsh-model-api',
  factory: require => {
    const { createElement: h, useState, useEffect, useRef } = require('react');
    const protocols = ['openai-completions', 'openai-responses', 'anthropic-messages'];
    const plain = { font: 'inherit', padding: '7px 10px', border: '1px solid #dce1e9', borderRadius: 8, background: '#fff', color: '#17304a' };
    const fieldStyle = { ...plain, width: '100%', boxSizing: 'border-box' };

    function validateURL(raw, type) {
      if (typeof raw !== 'string' || !raw || raw.trim() !== raw || raw.includes('?') || raw.includes('#')) return false;
      try {
        const url = new URL(raw);
        const authority = raw.match(/^[a-z]+:\/\/([^/?#]+)/i)?.[1];
        if (!authority || authority.includes('@') || url.username || url.password || url.search || url.hash) return false;
        if (type === 'cloud') return url.protocol === 'https:';
        if (type !== 'local' || !['http:', 'https:'].includes(url.protocol)) return false;
        return /^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(authority);
      } catch { return false; }
    }

    function credentialRef() {
      return 'JOHNASON_MODEL_API_' + crypto.randomUUID().replace(/-/g, '_').toUpperCase();
    }

    function createController(ctx) {
      let namespace;
      let writable = false;
      let rows = [];
      function newDraft(values = {}) {
        return { type: 'local', slug: '', displayName: '', baseURL: '', api: 'openai-completions', modelId: '', key: '', ...values, credentialRef: credentialRef() };
      }
      async function load() {
        const response = await ctx.remote.settings.describe();
        if (!response.ok) throw new Error('无法读取模型设置，请刷新重试。');
        writable = response.value.writable === true;
        namespace = response.value.namespaces.find(row => row.ns === 'llm-pi-ai');
        if (!namespace || !Number.isSafeInteger(namespace.revision)) throw new Error('模型设置尚未就绪，请刷新重试。');
        const providers = namespace.value?.providers;
        const entries = providers && typeof providers === 'object' && !Array.isArray(providers) ? Object.entries(providers) : [];
        rows = entries.filter(([route]) => route.startsWith('johnason-')).map(([route, profile]) => ({
          route,
          displayName: typeof profile?.displayName === 'string' ? profile.displayName : route,
          api: protocols.includes(profile?.api) ? profile.api : '未知协议',
          models: Array.isArray(profile?.models) ? profile.models.filter(row => typeof row?.id === 'string').map(row => row.id) : [],
          ref: typeof profile?.apiKeyEnv === 'string' ? profile.apiKeyEnv : undefined,
        }));
        const refs = [...new Set(rows.map(row => row.ref).filter(Boolean))];
        const credentials = {};
        const unavailableRefs = new Set();
        for (let index = 0; index < refs.length; index += 64) {
          const batch = refs.slice(index, index + 64);
          const status = await ctx.remote.credentials.describe(batch);
          if (status.ok) Object.assign(credentials, status.value);
          else for (const ref of batch) unavailableRefs.add(ref);
        }
        return { writable, revision: namespace.revision, rows: rows.map(({ ref, ...row }) => ({
          ...row,
          credentialStatus: !ref ? '未关联凭据' : unavailableRefs.has(ref) ? '凭据状态不可用'
            : credentials[ref]?.configured ? '凭据已配置' : credentials[ref]?.writable === false ? 'Vault 已锁定，请前往 Vault 解锁' : '凭据未配置',
        })) };
      }
      async function save(draft) {
        const base = namespace;
        if (!base) return { ok: false, message: '请先刷新列表。' };
        const slug = draft.slug?.trim();
        const route = 'johnason-' + slug;
        if (!/^[a-z][a-z0-9-]*$/.test(slug)) return { ok: false, message: '连接 ID 须以小写字母开头，只能包含小写字母、数字和连字符。' };
        if (!draft.displayName?.trim() || !draft.modelId?.trim()) return { ok: false, message: '请填写显示名称和模型 ID。' };
        if (!validateURL(draft.baseURL, draft.type)) return { ok: false, message: 'API 地址无效：本地只允许 loopback HTTP/HTTPS，云端只允许 HTTPS，且不能包含用户信息、查询参数或片段。' };
        if (!protocols.includes(draft.api)) return { ok: false, message: '请选择受支持的协议。' };
        if (draft.type === 'cloud' && !draft.key) return { ok: false, message: '云端连接必须填写 API Key。' };
        if (!/^[A-Z][A-Z0-9_]*$/.test(draft.credentialRef)) return { ok: false, message: '请重新填写连接信息。' };
        if (!writable) return { ok: false, message: '当前模型设置不可写，请刷新后重试。' };
        if (Object.hasOwn(base.value?.providers ?? {}, route)) return { ok: false, message: '连接 ID 已存在，请另选 ID。' };
        const key = draft.key || crypto.randomUUID();
        try {
          const status = await ctx.remote.credentials.describe([draft.credentialRef]);
          if (!status.ok) return { ok: false, message: '无法读取 Vault 状态，请前往 Vault 检查后重试。' };
          if (status.value[draft.credentialRef]?.writable !== true) return { ok: false, message: 'Vault 已锁定或不可写，请前往 Vault 解锁后重试。' };
          const stored = await ctx.remote.credentials.set(draft.credentialRef, key);
          if (!stored.ok) return { ok: false, message: '无法保存 API Key，请前往 Vault 检查解锁状态后重试。' };
          const profile = { displayName: draft.displayName.trim(), api: draft.api, baseURL: draft.baseURL, models: [{ id: draft.modelId.trim(), maxTokens: 4096 }], apiKeyEnv: draft.credentialRef };
          const written = await ctx.remote.settings.mutate('llm-pi-ai', [{ op: 'set', path: ['providers', route], value: profile }], base.revision);
          if (!written.ok) return { ok: false, message: '模型设置写入失败或版本已变化。凭据已保留，请刷新列表后重试。' };
          draft.key = '';
          return { ok: true, message: '连接已保存。' };
        } catch {
          return { ok: false, message: '保存失败，请检查 Vault 并刷新列表后重试。' };
        }
      }
      return { newDraft, load, save };
    }

    function Panel({ controller }) {
      const [view, setView] = useState({ rows: [], writable: false });
      const [draft, setDraft] = useState(() => controller.newDraft());
      const [busy, setBusy] = useState(false);
      const [message, setMessage] = useState('');
      const [messageError, setMessageError] = useState(false);
      const saving = useRef(false);
      async function refresh() {
        setBusy(true);
        try { setView(await controller.load()); setMessage(''); setMessageError(false); }
        catch { setMessage('无法读取模型连接，请刷新列表重试。'); setMessageError(true); }
        finally { setBusy(false); }
      }
      useEffect(() => { void refresh(); }, []);
      function change(name, value) {
        if (['type', 'slug', 'baseURL'].includes(name)) {
          setDraft(controller.newDraft({ ...draft, [name]: value, key: '' }));
        } else setDraft({ ...draft, [name]: value });
      }
      async function save() {
        if (saving.current) return;
        saving.current = true;
        setBusy(true);
        try {
          const result = await controller.save(draft);
          setMessage(result.message);
          setMessageError(!result.ok);
          if (result.ok) {
            try { setView(await controller.load()); }
            catch { setMessage('连接已保存，列表刷新失败。请点击“刷新列表”重试。'); setMessageError(true); }
            setDraft(controller.newDraft());
          } else setDraft({ ...draft });
        } finally { saving.current = false; setBusy(false); }
      }
      const input = (label, name, type = 'text') => h('label', { style: { display: 'grid', gap: 5 } }, label,
        h('input', { 'aria-label': label, type, value: draft[name], disabled: busy, onChange: event => change(name, event.target.value), style: fieldStyle, autoComplete: type === 'password' ? 'new-password' : 'off', ...name === 'baseURL' ? { placeholder: 'http://127.0.0.1:1234/v1' } : {} }));
      return h('section', { 'data-johnason-model-api': true, style: { display: 'grid', gap: 14, color: '#17304a', colorScheme: 'light', maxWidth: 720 } },
        h('h2', { style: { margin: 0 } }, '模型 API'),
        h('p', { style: { margin: 0 } }, '新增模型连接；API Key 保存在 Vault。仅点击“保存连接”时写入，不会自动探测网络。保存后可在原生模型选择器中选择。'),
        h('a', { href: '/vault' }, '打开 Vault'),
        h('div', { style: { display: 'grid', gap: 8 } },
          h('label', { style: { display: 'grid', gap: 5 } }, '连接类型', h('select', { 'aria-label': '连接类型', value: draft.type, disabled: busy, onChange: event => change('type', event.target.value), style: fieldStyle },
            h('option', { value: 'cloud' }, '云端'), h('option', { value: 'local' }, '本地'))),
          input('连接 ID', 'slug'), h('small', null, '实际路由：johnason-' + (draft.slug || '…')),
          input('显示名称', 'displayName'), input('API 地址', 'baseURL'),
          h('label', { style: { display: 'grid', gap: 5 } }, '协议', h('select', { 'aria-label': '协议', value: draft.api, disabled: busy, onChange: event => change('api', event.target.value), style: fieldStyle },
            ...protocols.map(value => h('option', { key: value, value }, value)))),
          input('模型 ID', 'modelId'), input('API Key', 'key', 'password')),
        draft.type === 'local' && h('small', null, '本地连接可留空 API Key；保存时会生成随机占位凭据并写入 Vault。'),
        message && h('p', { role: messageError ? 'alert' : 'status', style: { margin: 0 } }, message),
        h('div', { style: { display: 'flex', gap: 8 } },
          h('button', { type: 'button', disabled: busy || !view.writable, onClick: save, style: plain }, busy ? '保存中…' : '保存连接'),
          h('button', { type: 'button', disabled: busy, onClick: refresh, style: plain }, '刷新列表')),
        h('h3', { style: { marginBottom: 0 } }, '已配置连接'),
        view.rows.length ? h('ul', { style: { margin: 0, paddingLeft: 20 } }, ...view.rows.map(row => h('li', { key: row.route },
          h('strong', null, row.displayName), '（', h('span', null, row.route), '） · ' + row.api + ' · ' + row.models.join('、') + ' · ' + row.credentialStatus)))
          : h('p', { style: { margin: 0 } }, '暂无 Johnason 模型连接。'));
    }

    return {
      inject: ['slots', 'remote', 'remote.settings', 'remote.credentials'],
      createController,
      validateURL,
      apply(ctx) {
        const controller = createController(ctx);
        ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({ name: 'plugins.bundle.config', key: '@johnason/dsh-model-api' },
          ({ view }) => view === 'summary' ? '新增本地或云端模型 API 连接，密钥保存在 Vault。' : h(Panel, { controller })));
        ctx.slots.inject('settings.models.footer', () => ctx.slots.register({ name: 'settings.models.footer', id: 'johnason-model-api', order: 100 },
          () => h(Panel, { controller })));
      },
    };
  },
});

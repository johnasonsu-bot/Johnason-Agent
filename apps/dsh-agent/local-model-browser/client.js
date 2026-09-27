window.__ModuleLoader__.load({
  id: '@johnason/dsh-local-models',
  factory: require => {
    const { createElement: h, useState, useEffect, useRef } = require('react');
    const namespaceName = 'johnason-local-models';
    const defaults = {
      enabled: true,
      baseURL: 'http://127.0.0.1:1234/v1',
      apiKeyRef: '',
      decisionModel: 'jev-style-qwen3.5-2b-decision',
      workerModel: 'qwen3.8-27b-uncensored-mlx',
      timeoutMs: 60000,
      maxTokens: 2048,
      threshold: 0.75,
      maxCallsPerTurn: 8,
      maxConcurrent: 2,
    };
    const writableFields = Object.keys(defaults);
    const plain = { font: 'inherit', padding: '7px 10px', border: '1px solid #dce1e9', borderRadius: 8, background: '#fff', color: '#17304a' };
    const fieldStyle = { ...plain, width: '100%', boxSizing: 'border-box' };

    function validLocalURL(raw) {
      if (typeof raw !== 'string' || !raw || raw.trim() !== raw || raw.includes('?') || raw.includes('#')) return false;
      try {
        const url = new URL(raw);
        const authority = raw.match(/^https?:\/\/([^/?#]+)/i)?.[1];
        return !!authority && !authority.includes('@') && !url.username && !url.password && !url.search && !url.hash
          && ['http:', 'https:'].includes(url.protocol)
          && /^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(authority);
      } catch { return false; }
    }

    function validModelId(id) {
      return typeof id === 'string' && id.trim() === id && id.length > 0 && id.length <= 128 && !/[\r\n]/.test(id);
    }

    function validInteger(value, min, max) {
      return Number.isSafeInteger(value) && value >= min && value <= max;
    }

    function createController(ctx) {
      let namespace;
      let writable = false;
      function credentialRef() {
        return 'JOHNASON_LOCAL_MODEL_' + crypto.randomUUID().replace(/-/g, '_').toUpperCase();
      }
      function newDraft(value = {}) {
        const projected = Object.fromEntries(writableFields.filter(field => field !== 'apiKeyRef').map(field => [field, value[field] ?? defaults[field]]));
        return { ...projected, apiKey: '', noAuth: false, credentialRef: credentialRef() };
      }
      function changeDraft(draft, field, value) {
        if (field === 'baseURL' && value !== draft.baseURL) {
          return { ...draft, baseURL: value, apiKey: '', credentialRef: credentialRef() };
        }
        return { ...draft, [field]: value };
      }
      async function load() {
        const response = await ctx.remote.settings.describe();
        if (!response.ok) throw new Error('无法读取本地模型设置。');
        writable = response.value.writable === true;
        namespace = response.value.namespaces.find(row => row.ns === namespaceName);
        if (!namespace || !Number.isSafeInteger(namespace.revision)) throw new Error('本地模型设置尚未就绪。');
        const value = namespace.value ?? {};
        return { writable, revision: namespace.revision, draft: newDraft(value), hasCredential: typeof value.apiKeyRef === 'string' && !!value.apiKeyRef };
      }
      async function save(draft) {
        const base = namespace;
        if (!base) return { ok: false, message: '请先刷新配置。' };
        if (!writable) return { ok: false, message: '本地模型设置不可写，请刷新后重试。' };
        if (!validLocalURL(draft.baseURL)) return { ok: false, message: 'LM Studio API 地址必须是本机 loopback HTTP/HTTPS 地址，且不含用户信息、查询参数或片段。' };
        if (!validModelId(draft.decisionModel) || !validModelId(draft.workerModel)) return { ok: false, message: '请填写有效的模型 ID（不超过 128 字符）。' };
        if (typeof draft.enabled !== 'boolean' || typeof draft.noAuth !== 'boolean'
          || typeof draft.threshold !== 'number' || !Number.isFinite(draft.threshold) || draft.threshold < 0 || draft.threshold > 1
          || !validInteger(draft.timeoutMs, 1000, 120000) || !validInteger(draft.maxTokens, 64, 4096)
          || !validInteger(draft.maxCallsPerTurn, 1, 32) || !validInteger(draft.maxConcurrent, 1, 4)) {
          return { ok: false, message: '请检查阈值、超时、输出上限、调用预算和并发上限。' };
        }
        const key = typeof draft.apiKey === 'string' ? draft.apiKey : '';
        const oldRef = typeof base.value?.apiKeyRef === 'string' ? base.value.apiKeyRef : '';
        const unchangedURL = draft.baseURL === base.value?.baseURL;
        let apiKeyRef = '';
        if (!draft.noAuth && key) apiKeyRef = draft.credentialRef;
        else if (!draft.noAuth && unchangedURL) apiKeyRef = oldRef;
        if (apiKeyRef && !/^[A-Z][A-Z0-9_]*$/.test(apiKeyRef)) return { ok: false, message: '凭据引用无效，请刷新配置后重试。' };
        try {
          if (apiKeyRef) {
            const status = await ctx.remote.credentials.describe([apiKeyRef]);
            if (!status.ok || status.value?.[apiKeyRef]?.writable !== true) {
              return { ok: false, message: 'Vault 已锁定或不可写，请前往 Vault 解锁后重试。' };
            }
            if (key && !draft.noAuth) {
              const stored = await ctx.remote.credentials.set(apiKeyRef, key);
              if (!stored.ok) return { ok: false, message: '无法保存 API Key，请检查 Vault 后重试。' };
            }
          }
          const values = {
            enabled: draft.enabled, baseURL: draft.baseURL, apiKeyRef,
            decisionModel: draft.decisionModel, workerModel: draft.workerModel,
            timeoutMs: draft.timeoutMs, maxTokens: draft.maxTokens, threshold: draft.threshold,
            maxCallsPerTurn: draft.maxCallsPerTurn, maxConcurrent: draft.maxConcurrent,
          };
          const operations = writableFields.map(field => ({ op: 'set', path: [field], value: values[field] }));
          const written = await ctx.remote.settings.mutate(namespaceName, operations, base.revision);
          if (!written.ok) return { ok: false, message: '本地模型设置写入失败或版本已变化，请刷新配置后重试。' };
          draft.apiKey = '';
          return { ok: true, message: '混合配置已保存。' };
        } catch {
          return { ok: false, message: '保存失败，请检查 Vault 或刷新配置后重试。' };
        }
      }
      async function listModels() {
        try {
          const response = await fetch('/johnason/local-models/models', { method: 'GET', credentials: 'same-origin' });
          if (!response.ok) return { ok: false, message: '无法读取本地模型，请检查 LM Studio 服务。' };
          const data = await response.json();
          if (!Array.isArray(data?.models)) return { ok: false, message: '本地模型列表格式无效。' };
          const models = data.models.filter(row => validModelId(row?.id)).map(row => ({ id: row.id }));
          return {
            ok: true, models,
            decisionModel: validModelId(data.decisionModel) ? data.decisionModel : '',
            workerModel: validModelId(data.workerModel) ? data.workerModel : '',
          };
        } catch {
          return { ok: false, message: '无法读取本地模型，请检查 LM Studio 服务。' };
        }
      }
      return { newDraft, changeDraft, load, save, listModels };
    }

    function Panel({ controller }) {
      const [view, setView] = useState({ writable: false, hasCredential: false });
      const [draft, setDraft] = useState(() => controller.newDraft());
      const [models, setModels] = useState([]);
      const [busy, setBusy] = useState(true);
      const [message, setMessage] = useState('');
      const [error, setError] = useState(false);
      const working = useRef(false);
      async function refresh() {
        if (working.current) return;
        working.current = true;
        setBusy(true);
        try {
          const loaded = await controller.load();
          setView(loaded); setDraft(loaded.draft); setMessage(''); setError(false);
        } catch { setMessage('无法读取本地模型设置，请重试。'); setError(true); }
        finally { working.current = false; setBusy(false); }
      }
      useEffect(() => { void refresh(); }, []);
      function change(field, value) {
        if (working.current) return;
        setDraft(current => controller.changeDraft(current, field, value));
      }
      async function save() {
        if (working.current) return;
        working.current = true;
        setBusy(true);
        try {
          const result = await controller.save(draft);
          setMessage(result.message); setError(!result.ok);
          if (result.ok) {
            try {
              const loaded = await controller.load();
              setView(loaded); setDraft(loaded.draft);
            } catch { setDraft({ ...draft }); setMessage('配置已保存，刷新失败。请点击“刷新配置”重试。'); setError(true); }
          } else setDraft({ ...draft });
        } finally { working.current = false; setBusy(false); }
      }
      async function readModels() {
        if (working.current) return;
        working.current = true;
        setBusy(true);
        try {
          const result = await controller.listModels();
          if (result.ok) { setModels(result.models); setMessage('已读取本地模型列表。'); setError(false); }
          else { setMessage(result.message); setError(true); }
        } finally { working.current = false; setBusy(false); }
      }
      const input = (label, field, type = 'text', attrs = {}) => h('label', { style: { display: 'grid', gap: 5 } }, label,
        h('input', { type, 'aria-label': label, value: draft[field], disabled: busy, onChange: event => change(field, type === 'number' ? Number(event.target.value) : event.target.value), style: fieldStyle,
          autoComplete: type === 'password' ? 'new-password' : 'off', ...attrs }));
      return h('section', { 'data-johnason-local-models': true, style: { display: 'grid', gap: 12, maxWidth: 720, color: '#17304a', colorScheme: 'light' } },
        h('h2', { style: { margin: 0 } }, '本地模型与 Jev 混合调用'),
        h('p', { style: { margin: 0 } }, '主 Agent 可通过 Skill johnason-local-models 调用 local_model_list、local_model_generate 和 jev_route_task。Jev 将简单文本任务交给本地工作模型；复杂任务、低置信判断或调用失败时由主 Agent 继续处理。决策置信度是模型自报值，并非校准概率。此配置不更改会话模型或权限。'),
        h('a', { href: '/vault', target: '_blank', rel: 'noopener noreferrer' }, '打开 Vault'),
        h('label', null, h('input', { type: 'checkbox', 'aria-label': '启用本地模型工具', checked: draft.enabled, disabled: busy, onChange: event => change('enabled', event.target.checked) }), '启用本地模型工具'),
        input('LM Studio API 地址', 'baseURL'),
        input('Jev 决策模型', 'decisionModel', 'text', { list: 'johnason-local-model-ids' }),
        input('本地工作模型', 'workerModel', 'text', { list: 'johnason-local-model-ids' }),
        h('datalist', { id: 'johnason-local-model-ids' }, ...models.map(model => h('option', { key: model.id, value: model.id }))),
        input('LM Studio API Key', 'apiKey', 'password'),
        h('label', null, h('input', { type: 'checkbox', 'aria-label': '无需认证', checked: draft.noAuth, disabled: busy, onChange: event => change('noAuth', event.target.checked) }), '无需认证（清除当前配置的凭据引用；Vault 中的凭据保留）'),
        h('small', null, view.hasCredential ? '当前配置已关联 Vault 凭据。' : '本地无认证服务可留空 API Key。'),
        input('决策阈值', 'threshold', 'number', { min: 0, max: 1, step: 0.01 }),
        input('调用超时（毫秒）', 'timeoutMs', 'number', { min: 1000, max: 120000, step: 1 }),
        input('输出上限', 'maxTokens', 'number', { min: 64, max: 4096, step: 1 }),
        input('每轮调用预算', 'maxCallsPerTurn', 'number', { min: 1, max: 32, step: 1 }),
        input('并发上限', 'maxConcurrent', 'number', { min: 1, max: 4, step: 1 }),
        message && h('p', { role: error ? 'alert' : 'status', style: { margin: 0 } }, message),
        h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
          h('button', { type: 'button', disabled: busy || !view.writable, onClick: save, style: plain }, '保存混合配置'),
          h('button', { type: 'button', disabled: busy, onClick: refresh, style: plain }, '刷新配置'),
          h('button', { type: 'button', disabled: busy, onClick: readModels, style: plain }, '读取本地模型')),
        models.length > 0 && h('div', null, h('h3', null, '本地可用模型'),
          h('ul', null, ...models.map(model => h('li', { key: model.id }, model.id)))));
    }

    return {
      inject: ['slots', 'remote', 'remote.settings', 'remote.credentials'],
      createController,
      apply(ctx) {
        const controller = createController(ctx);
        ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({ name: 'plugins.bundle.config', key: '@johnason/dsh-local-models' },
          ({ view }) => view === 'summary' ? '本地模型与 Jev 混合调用' : h(Panel, { controller })));
      },
    };
  },
});

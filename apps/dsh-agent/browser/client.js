/* Native DSH module-loader factory: deliberately dependency/build-step free. */
window.__ModuleLoader__.load({
  id: '@johnason/dsh-memory-ui',
  factory: require => {
    const { createElement: h, useState, useEffect, useRef } = require('react');
    async function api(action, data) {
      const response = await fetch('/memory-recovery/api/' + action, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(data),
      });
      const body = await response.json();
      if (!response.ok) throw Object.assign(new Error(body.error || '记忆服务暂不可用，请刷新。'), { code: body.code });
      return body.value;
    }
    const control = { font: 'inherit', padding: '7px 10px', border: '1px solid #dce1e9', borderRadius: 8, background: '#fff', color: '#17304a', lineHeight: 1.5, opacity: 1 };
    const field = { ...control, width: '100%', boxSizing: 'border-box' };
    function LocalPluginPanel() {
      const [metadata, setMetadata] = useState({ state: 'loading' });
      const [vault, setVault] = useState({ state: 'loading' });
      const [memory, setMemory] = useState({ state: 'loading' });
      const [busy, setBusy] = useState(false);
      const active = useRef(false), serial = useRef(0), pending = useRef(null);
      async function refresh() {
        pending.current?.abort();
        const controller = new AbortController();
        pending.current = controller;
        const seq = ++serial.current;
        setBusy(true);
        const timeout = setTimeout(() => controller.abort(), 10000);
        async function load(url, options, project, setState) {
          setState({ state: 'loading' });
          try {
            const response = await fetch(url, { ...options, signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
            if (!response.ok) throw new Error('UNAVAILABLE');
            const safe = project(await response.json());
            if (active.current && serial.current === seq) setState({ state: 'ready', ...safe });
          } catch {
            if (active.current && serial.current === seq) setState({ state: 'error' });
          }
        }
        try {
          await Promise.all([
            load('/johnason/plugins', {}, value => {
              if (value.schemaVersion !== 1 || !Array.isArray(value.providers)) throw new Error('INVALID_METADATA');
              return { providers: value.providers.map(({ id, label, type }) => ({ id, label, type })) };
            }, setMetadata),
            load('/vault/status', {}, value => {
              if (typeof value.initialized !== 'boolean' || typeof value.locked !== 'boolean') throw new Error('INVALID_STATUS');
              return { initialized: value.initialized, locked: value.locked };
            }, setVault),
            load('/memory-recovery/api/sessions', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }, value => {
              if (!Array.isArray(value.value)) throw new Error('INVALID_STATUS');
              // Retain counts only: the overview never displays session identity, paths or memory content.
              return { enabled: value.value.filter(row => row.config?.enabled).length, sandboxed: value.value.filter(row => row.config?.sandbox).length };
            }, setMemory),
          ]);
        } finally {
          clearTimeout(timeout);
          if (active.current && serial.current === seq) setBusy(false);
        }
      }
      useEffect(() => {
        active.current = true; refresh();
        return () => { active.current = false; ++serial.current; pending.current?.abort(); };
      }, []);
      const availability = (source, ready) => source.state === 'loading' ? '加载中…' : source.state === 'error' ? '服务不可用' : ready;
      const statusStyle = { fontSize: 13, fontWeight: 600, color: '#40546d' };
      const card = (title, status, description, href, link) => h('article', { key: title, style: { border: '1px solid #dce1e9', borderRadius: 12, padding: 16, display: 'grid', gap: 10, alignContent: 'start', background: '#fff' } },
        h('h3', { style: { margin: 0, fontSize: 16 } }, title),
        h('span', { style: statusStyle, role: 'status' }, status),
        h('p', { style: { margin: 0, color: '#526174', lineHeight: 1.6 } }, description),
        href && h('a', { href, target: '_blank', rel: 'noopener noreferrer', style: { color: '#315cbe' } }, link));
      const providers = metadata.providers || [];
      const systems = (title, type) => {
        const configured = providers.filter(provider => provider.type === type);
        return card(title, availability(metadata, configured.length ? '已配置' : '未配置'),
          metadata.state === 'error' ? '无法读取系统配置状态，请刷新重试。'
            : metadata.state !== 'ready' ? '正在读取本地系统配置。'
            : configured.length ? configured.map(provider => provider.label).join('、') + '。请进入系统管理查看授权状态或登录。'
            : title + ' 连接尚未配置。需在本地系统配置中指定服务地址及对应核心适配器，然后重新启动；凭据通过 Vault 保存。',
          metadata.state === 'ready' && configured.length ? '/systems' : null, '打开系统管理');
      };
      const memoryStatus = availability(memory, memory.enabled + ' 个会话已启用');
      return h('section', { 'data-johnason-plugins': true, style: { color: '#17304a', colorScheme: 'light', lineHeight: 1.5, display: 'grid', gap: 16, padding: '8px 0' } },
        h('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 } },
          h('h2', { style: { margin: 0, fontSize: 20 } }, 'Johnason 本地功能'),
          h('button', { type: 'button', style: control, disabled: busy, onClick: refresh }, busy ? '刷新中…' : '刷新状态')),
        h('p', { style: { margin: 0, color: '#526174' } }, '此插件提供本地功能入口。停用入口不会移除已启用的会话约束；Vault、记忆和沙箱的服务策略继续由宿主管理。'),
        h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 260px), 1fr))', gap: 14 } },
          card('Vault 凭据', availability(vault, !vault.initialized ? '未初始化' : vault.locked ? '已锁定' : '已解锁'), '管理加密保存的模型与系统凭据；在 Vault 页面初始化、解锁或锁定。', '/vault', '打开 Vault'),
          card('三类记忆', memoryStatus, '按会话管理情景记忆、语义记忆与程序记忆。新会话可在发送首条消息前启用。', '/memory-recovery', '管理三类记忆'),
          card('恢复审计', memoryStatus, '查看记忆持久化、执行效果与恢复审计记录。只显示已有会话的记录，不自动导入旧版历史。', '/memory-recovery', '查看恢复审计'),
          card('语义子图', memoryStatus, '按会话查看语义关系与时间范围，追踪记忆来源。', '/memory-recovery', '查看语义子图'),
          card('任务沙箱', availability(memory, memory.sandboxed + ' 个会话已启用'), '按会话显式启用，当前并非全局开启。需在会话配置中选择策略；已启用的沙箱不能移除或放宽。', '/memory-recovery', '配置会话沙箱'),
          systems('Forge DevOps', 'forge'), systems('Data Platform', 'dataplatform'),
          card('统一授权', availability(metadata, providers.length ? '已配置' : '未配置'),
            metadata.state === 'error' ? '无法读取系统配置状态，请刷新重试。'
              : metadata.state !== 'ready' ? '正在读取本地系统配置。'
              : providers.length ? '统一管理已配置系统的登录与授权。配置完成不代表已登录，请进入系统管理确认。' : '需先配置 Forge DevOps 或 Data Platform 连接，再通过系统管理登录与授权。',
            metadata.state === 'ready' && providers.length ? '/systems' : null, '管理统一授权')));
    }
    function MemoryDock({ sessionId }) {
      const [open, setOpen] = useState(false), [config, setConfig] = useState(null), [status, setStatus] = useState(null);
      const [error, setError] = useState(''), [busy, setBusy] = useState(false), [stale, setStale] = useState(false);
      const [project, setProject] = useState(''), [workspace, setWorkspace] = useState('');
      const [budget, setBudget] = useState(4000), [anchors, setAnchors] = useState('');
      const [sandbox, setSandbox] = useState(false), [mode, setMode] = useState('workspace-write');
      const active = useRef(false), serial = useRef(0), saving = useRef(false), baseVersion = useRef(null);
      async function refresh(fill = false) {
        const seq = ++serial.current;
        try {
          const c = await api('config', { sessionId });
          const rows = !c.workspaceRoot ? await api('sessions', {}) : [];
          const s = await api('status', { sessionId });
          if (!active.current || seq !== serial.current) return;
          if (!fill && baseVersion.current !== null && c.version !== baseVersion.current) {
            setStale(true); setStatus(s); setError('配置已在其他页面更新。请点击“刷新状态”载入最新配置后再编辑；当前草稿不会覆盖它。'); return;
          }
          setConfig(c); setStatus(s); setError('');
          setWorkspace(c.workspaceRoot || rows.find(row => row.id === sessionId)?.cwd || '');
          if (fill) { baseVersion.current = c.version; setStale(false); setProject(c.projectId || ''); setBudget(c.budgetTokens || 4000); setAnchors((c.anchors || []).join('\n')); setSandbox(!!c.sandbox); setMode(c.sandbox?.mode || 'workspace-write'); }
        } catch (e) { if (active.current && seq === serial.current) setError(e.message); }
      }
      useEffect(() => {
        active.current = true; refresh(true);
        const focus = () => { if (!saving.current) refresh(false); };
        window.addEventListener('focus', focus);
        return () => { active.current = false; ++serial.current; window.removeEventListener('focus', focus); };
      }, [sessionId]);
      async function save() {
        if (saving.current || stale || !config?.canConfigure) return;
        if (!project.trim() || !workspace || !Number.isInteger(Number(budget)) || Number(budget) < 128 || Number(budget) > 100000) {
          setError('请填写项目 ID，并使用 128–100000 的整数上下文预算。'); return;
        }
        saving.current = true; setBusy(true); ++serial.current;
        try {
          await api('configure', { sessionId, expectedVersion: config.version, input: {
            enabled: true, projectId: project.trim(), agentId: sessionId, workspaceRoot: workspace,
            budgetTokens: Number(budget), anchors: anchors.split('\n').filter(s => s.trim()),
            sandbox: sandbox ? { sandbox_required: true, mode, network: 'host', workspaceRoot: workspace } : null,
          } });
          if (active.current) await refresh(true);
        } catch (e) { if (active.current) setError(e.message); }
        finally { saving.current = false; if (active.current) setBusy(false); }
      }
      const label = error || status?.error ? '记忆：异常' : !config ? '记忆：加载中' : config.enabled ? '记忆：已启用' : '记忆：未启用';
      const block = config?.configurationBlockReason === 'MEMORY_NEW_SESSION_REQUIRED' ? '此会话已有历史，不能首次启用记忆。请新建会话，并在发送第一条消息前启用；旧历史保持不变。' : config?.canConfigure === false ? '当前会话暂不能修改配置，请等待执行完成后刷新。' : '';
      const input = (title, props) => h('label', { style: { display: 'grid', gap: 5 } }, title, h('input', { ...props, style: field, 'aria-label': title }));
      return h('section', { 'data-johnason-memory': sessionId, style: { width: '100%', minWidth: 0, padding: '6px 0', fontSize: 13, color: '#17304a', colorScheme: 'light' } },
        h('style', null, `[data-johnason-memory] input::placeholder,[data-johnason-memory] textarea::placeholder { color:#657084; opacity:1; }
          [data-johnason-memory] button { cursor:pointer; }
          [data-johnason-memory] button:disabled,[data-johnason-memory] input:disabled,[data-johnason-memory] select:disabled,[data-johnason-memory] textarea:disabled { color:#526174 !important; background:#edf1f5 !important; opacity:1 !important; cursor:not-allowed; -webkit-text-fill-color:#526174; }
          [data-johnason-memory] input:read-only { background:#f4f6f9 !important; }
          [data-johnason-memory] :focus-visible { outline:2px solid #2563eb; outline-offset:2px; }
          [data-memory-panel] > * { min-width:0; }
          [data-johnason-memory] textarea { resize:vertical; min-height:72px; }
          [data-johnason-memory] p { overflow-wrap:anywhere; }`),
        h('div', { style: { display: 'flex', justifyContent: 'flex-end', gap: 8, alignItems: 'center' } },
          h('button', { type: 'button', style: control, 'aria-expanded': open, onClick: () => { setOpen(!open); if (!open) refresh(false); } }, label),
          config?.enabled && h('a', { href: '/memory-recovery?sessionId=' + encodeURIComponent(sessionId), style: { color: '#315cbe' } }, '查看三类记忆')),
        open && h('div', { 'data-memory-panel': true, role: 'region', 'aria-label': '当前会话记忆配置', style: { maxHeight: '55dvh', overflowY: 'auto', overscrollBehavior: 'contain', boxSizing: 'border-box', color: '#17304a', lineHeight: 1.5, marginTop: 8, border: '1px solid #dce1e9', borderRadius: 12, padding: 16, background: '#fff', display: 'grid', gap: 12 } },
          h('strong', null, '当前会话 · 记忆配置'),
          h('small', { style: { overflowWrap: 'anywhere' } }, sessionId),
          error && h('div', { role: 'alert', style: { color: '#9f2424', background: '#fff1f1', padding: 10, borderRadius: 6 } }, error),
          block && h('p', { role: 'status', style: { margin: 0, background: '#edf3fc', color: '#24466b', padding: 10, borderRadius: 6 } }, block),
          input('记忆项目 ID', { value: project, disabled: !!config?.enabled || busy, onChange: e => setProject(e.target.value), placeholder: '例如 project-alpha' }),
          input('工作目录', { value: workspace, readOnly: true }),
          input('上下文预算', { type: 'number', min: 128, max: 100000, value: budget, disabled: busy, onChange: e => setBudget(e.target.value) }),
          h('label', null, '常驻锚点（每行一条）', h('textarea', { 'aria-label': '常驻锚点', style: field, rows: 2, value: anchors, disabled: busy, onChange: e => setAnchors(e.target.value) })),
          h('label', { style: { display: 'flex', gap: 8, alignItems: 'center' } }, h('input', { type: 'checkbox', 'aria-label': '独立强制沙箱', checked: sandbox, disabled: !!config?.sandbox || busy, onChange: e => setSandbox(e.target.checked) }), '独立强制沙箱（可选，与启用记忆分开）'),
          sandbox && h('select', { 'aria-label': '沙箱模式', style: field, value: mode, disabled: !!config?.sandbox || busy, onChange: e => setMode(e.target.value) }, h('option', { value: 'workspace-write' }, '工作区写入 · workspace-write'), h('option', { value: 'read-only' }, '只读 · read-only')),
          h('p', { style: { margin: 0, color: '#657084' } }, sandbox ? '强制沙箱仅提供受控记忆工具与 memory_sandbox_run；不提供原生 Bash / Glob。已启用的沙箱不会移除或放宽。' : '普通记忆保留原生 DSH 工具与权限；原生工具不具备 Effect 重放保护。不会执行模型或导入旧历史。'),
          h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap', position: 'sticky', bottom: -16, background: '#fff', padding: '12px 0', borderTop: '1px solid #dce1e9', zIndex: 1 } },
            h('button', { type: 'button', style: control, disabled: busy || stale || config?.canConfigure !== true, onClick: save }, busy ? '保存中…' : config?.enabled ? '保存记忆配置' : '启用当前会话记忆'),
            h('button', { type: 'button', style: control, disabled: busy, onClick: () => refresh(true) }, '刷新状态'),
            h('button', { type: 'button', style: control, onClick: () => setOpen(false) }, '收起')),
          config?.enabled && h('small', null, '配置 v' + config.version + ' · ' + config.executionCoverage + ' · 已持久化序号 ' + (status?.durableThroughSeq ?? '尚无') + ' · 待写入 ' + (status?.pending ?? 0)))
      );
    }
    return {
      inject: ['slots'],
      apply(ctx) {
        for (const name of ['sidebar.brand.mark', 'conversation.hero.brand.mark']) {
          ctx.slots.inject(name, () => ctx.slots.register({ name, priority: -10 }, () => null));
        }
        ctx.slots.inject('sidebar.brand.name', () => ctx.slots.register({ name: 'sidebar.brand.name', priority: -10 }, () => h('strong', null, 'Johnason Agent')));
        ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
          name: 'plugins.bundle.config', key: '@johnason/dsh-memory-ui',
        }, ({ view }) => view === 'summary' ? 'Vault、三类记忆、恢复审计、语义子图、任务沙箱与系统统一授权。' : h(LocalPluginPanel)));
        ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
          name: 'conversation.input.dock', id: 'johnason-memory', order: -20, inject: sessionId => ({ sessionId }),
        }, props => h(MemoryDock, { ...props, key: props.sessionId })));
      },
    };
  },
});

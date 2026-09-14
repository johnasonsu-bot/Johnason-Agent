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
        ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
          name: 'conversation.input.dock', id: 'johnason-memory', order: -20, inject: sessionId => ({ sessionId }),
        }, props => h(MemoryDock, { ...props, key: props.sessionId })));
      },
    };
  },
});

const contexts = [
  { value: "文件 · report.pdf", label: "文件 / 文件夹", icon: "▧" },
  { value: "插件 · Data Platform connector", label: "插件 / Connector", icon: "⌘" },
  { value: "会话 · 方案评审", label: "其他会话", icon: "◌" },
];

export function ContextMenu({ open, onSelect }: { open: boolean; onSelect: (value: string) => void }) {
  if (!open) return null;
  return <div className="composer-popover context-menu" role="menu" aria-label="添加到本轮上下文"><h4>添加到本轮上下文</h4>{contexts.map((context) => <button key={context.value} type="button" aria-label={context.value} onClick={() => onSelect(context.value)}><span>{context.icon}</span><span>{context.label}</span></button>)}</div>;
}

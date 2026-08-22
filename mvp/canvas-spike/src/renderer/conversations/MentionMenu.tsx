const mentions = [
  { token: "@产品经理 ", label: "@产品经理 · Agent", icon: "🔵" },
  { token: "@架构师 ", label: "@架构师 · Agent", icon: "🟣" },
  { token: "@工程师 ", label: "@工程师 · Agent", icon: "🟢" },
  { token: "@deep-research ", label: "@deep-research · Skill", icon: "✦" },
  { token: "@workspace.run ", label: "@workspace.run · Tool", icon: "▣" },
];

export function MentionMenu({ open, onSelect }: { open: boolean; onSelect: (token: string) => void }) {
  if (!open) return null;
  return <div className="composer-popover mention-menu" role="menu" aria-label="提及并安排任务"><h4>提及并安排任务</h4>{mentions.map((mention) => <button key={mention.token} type="button" aria-label={mention.label} onClick={() => onSelect(mention.token)}><span>{mention.icon}</span><span>{mention.label}</span></button>)}</div>;
}

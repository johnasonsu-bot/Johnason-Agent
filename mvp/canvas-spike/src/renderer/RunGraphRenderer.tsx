import type { RendererProps } from "./renderers";

export const RunGraphRenderer = ({ artifact }: RendererProps) => {
  const nodes = (artifact.data as { nodes?: string[] } | undefined)?.nodes ?? [];
  return (
    <svg data-testid={`artifact-${artifact.id}`} role="img" aria-label={artifact.title} width="420" height="90">
      {nodes.map((node, index) => <g key={node} transform={`translate(${index * 130 + 5} 20)`}><rect width="105" height="40" rx="8" fill="#e2e8f0" /><text x="52" y="25" textAnchor="middle">{node}</text>{index < nodes.length - 1 ? <path d="M105 20 H125" stroke="#0f172a" /> : null}</g>)}
    </svg>
  );
};

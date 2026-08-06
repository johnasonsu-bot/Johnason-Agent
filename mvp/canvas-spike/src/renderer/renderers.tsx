import type { ComponentType } from "react";

export interface ArtifactDescriptor {
  id: string;
  kind: string;
  mimeType: string;
  title: string;
  content?: string;
}

export interface RendererProps {
  artifact: ArtifactDescriptor;
}

export class RendererRegistry {
  private readonly renderers = new Map<string, ComponentType<RendererProps>>();

  register(kind: string, component: ComponentType<RendererProps>): void {
    this.renderers.set(kind, component);
  }

  resolve(kind: string): ComponentType<RendererProps> {
    return this.renderers.get(kind) ?? UnknownRenderer;
  }
}

const MarkdownRenderer = ({ artifact }: RendererProps) => (
  <article data-testid={`artifact-${artifact.id}`}><h2>{artifact.title}</h2><p>{artifact.content}</p></article>
);

const ChartRenderer = ({ artifact }: RendererProps) => (
  <svg data-testid={`artifact-${artifact.id}`} role="img" aria-label={artifact.title} width="240" height="120">
    <rect x="20" y="70" width="40" height="40" fill="#2563eb" />
    <rect x="90" y="40" width="40" height="70" fill="#06b6d4" />
    <rect x="160" y="15" width="40" height="95" fill="#0f172a" />
  </svg>
);

const HtmlRenderer = ({ artifact }: RendererProps) => (
  <iframe
    data-testid={`artifact-${artifact.id}`}
    title={artifact.title}
    sandbox=""
    srcDoc={artifact.content}
  />
);

const AudioRenderer = ({ artifact }: RendererProps) => (
  <audio
    data-testid={`artifact-${artifact.id}`}
    aria-label={artifact.title}
    controls
    autoPlay={false}
    src={artifact.content}
  />
);

const UnknownRenderer = ({ artifact }: RendererProps) => (
  <section data-testid={`artifact-${artifact.id}`}>
    <strong>No renderer</strong><span>{artifact.mimeType}</span>
  </section>
);

export const registry = new RendererRegistry();
registry.register("markdown", MarkdownRenderer);
registry.register("chart", ChartRenderer);
registry.register("html", HtmlRenderer);
registry.register("audio", AudioRenderer);


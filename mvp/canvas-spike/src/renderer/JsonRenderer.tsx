import type { RendererProps } from "./renderers";

export const JsonRenderer = ({ artifact }: RendererProps) => (
  <section data-testid={`artifact-${artifact.id}`}>
    <h2>{artifact.title}</h2><code>{artifact.mimeType}</code>
    <pre>{JSON.stringify(artifact.data, null, 2)}</pre>
  </section>
);

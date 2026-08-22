import type { RendererProps } from "./renderers";

export const MarkdownRenderer = ({ artifact }: RendererProps) => (
  <article data-testid={`artifact-${artifact.id}`}><h2>{artifact.title}</h2><p>{artifact.content}</p></article>
);

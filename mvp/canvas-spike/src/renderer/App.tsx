import { registry, type ArtifactDescriptor } from "./renderers";

const artifacts: ArtifactDescriptor[] = [
  { id: "markdown", kind: "markdown", mimeType: "text/markdown", title: "Report", content: "Phase 0 Canvas" },
  { id: "chart", kind: "chart", mimeType: "application/vnd.vega+json", title: "Validation chart" },
  { id: "html", kind: "html", mimeType: "text/html", title: "Sandboxed HTML", content: "<script>document.body.dataset.node=typeof require</script><p>Sandboxed</p>" },
  { id: "audio", kind: "audio", mimeType: "audio/wav", title: "Audio artifact", content: "data:audio/wav;base64,UklGRgQAAABXQVZF" },
  { id: "unknown", kind: "future", mimeType: "application/x-future", title: "Future artifact" },
];

export function App() {
  return (
    <main>
      <h1>Canvas Sandbox Probe</h1>
      {artifacts.map((artifact) => {
        const Renderer = registry.resolve(artifact.kind);
        return <Renderer key={artifact.id} artifact={artifact} />;
      })}
    </main>
  );
}


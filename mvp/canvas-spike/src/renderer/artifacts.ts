import type { ArtifactDescriptor } from "./renderers";

export const artifacts: ArtifactDescriptor[] = [
  { id: "markdown", kind: "markdown", mimeType: "text/markdown", title: "Report", content: "Phase 0 Canvas" },
  { id: "json", kind: "json", mimeType: "application/json", title: "Job status", data: { jobId: 73, state: "completed" } },
  { id: "table", kind: "table", mimeType: "application/vnd.workbench.table+json", title: "Result preview", data: [{ employee: "A", score: 0.91 }, { employee: "B", score: 0.86 }] },
  { id: "run-graph", kind: "run-graph", mimeType: "application/vnd.workbench.run-graph+json", title: "Run graph", data: { nodes: ["queued", "running", "completed"] } },
  { id: "chart", kind: "chart", mimeType: "application/vnd.vega+json", title: "Validation chart" },
  { id: "html", kind: "html", mimeType: "text/html", title: "Sandboxed HTML", content: "<script>document.body.dataset.node=typeof require</script><p>Sandboxed</p>" },
  { id: "audio", kind: "audio", mimeType: "audio/wav", title: "Audio artifact", content: "data:audio/wav;base64,UklGRgQAAABXQVZF" },
  { id: "unknown", kind: "future", mimeType: "application/x-future", title: "Future artifact" },
];

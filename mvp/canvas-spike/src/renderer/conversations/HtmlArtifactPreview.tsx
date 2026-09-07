import { useEffect, useMemo, useState } from "react";
import { artifactApi, type ArtifactContent } from "../api";

export function HtmlArtifactPreview({
  artifactId,
  filename = "animation.html",
  onDownload,
}: {
  artifactId: string;
  filename?: string;
  onDownload?: () => Promise<void>;
}) {
  const [artifact, setArtifact] = useState<ArtifactContent | null>(null);
  const [error, setError] = useState<{ artifactId: string; message: string } | null>(null);
  useEffect(() => {
    let active = true;
    setArtifact(null);
    setError(null);
    void artifactApi.read(artifactId).then((value) => { if (active) setArtifact(value); }).catch((reason: unknown) => { if (active) setError({ artifactId, message: reason instanceof Error ? reason.message : "Artifact 加载失败" }); });
    return () => { active = false; };
  }, [artifactId]);
  const visibleArtifact = artifact?.artifact_id === artifactId ? artifact : null;
  const visibleError = error?.artifactId === artifactId ? error.message : "";
  const download = useMemo(() => visibleArtifact ? `data:${visibleArtifact.media_type};charset=utf-8,${encodeURIComponent(visibleArtifact.content)}` : "", [visibleArtifact]);
  return <section className="html-artifact" aria-label="HTML Artifact 预览">
    <header><div><strong title={filename}>{filename}</strong><small>{artifactId.slice(0, 22)}…</small></div>{visibleArtifact && (onDownload
      ? <button type="button" className="quiet" aria-label={`下载 ${filename}`} onClick={() => void onDownload()}>下载</button>
      : <a href={download} download={filename} aria-label={`下载 ${filename}`}>下载</a>)}</header>
    {visibleError && <p role="alert">{visibleError}</p>}
    {visibleArtifact && <iframe title={filename} sandbox="allow-scripts" srcDoc={visibleArtifact.content} />}
  </section>;
}

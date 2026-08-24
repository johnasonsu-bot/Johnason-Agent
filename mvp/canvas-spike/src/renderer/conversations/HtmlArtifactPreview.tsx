import { useEffect, useMemo, useState } from "react";
import { artifactApi, type ArtifactContent } from "../api";

export function HtmlArtifactPreview({ artifactId }: { artifactId: string }) {
  const [artifact, setArtifact] = useState<ArtifactContent | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void artifactApi.read(artifactId).then((value) => { if (active) setArtifact(value); }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Artifact 加载失败"); });
    return () => { active = false; };
  }, [artifactId]);
  const download = useMemo(() => artifact ? `data:${artifact.media_type};charset=utf-8,${encodeURIComponent(artifact.content)}` : "", [artifact]);
  return <section className="html-artifact" aria-label="HTML Artifact 预览">
    <header><div><strong title="animation.html">animation.html</strong><small>{artifactId.slice(0, 22)}…</small></div>{artifact && <a href={download} download="animation.html">下载</a>}</header>
    {error && <p role="alert">{error}</p>}
    {artifact && <iframe title="animation.html" sandbox="allow-scripts" srcDoc={artifact.content} />}
  </section>;
}

import { useEffect, useRef, useState, type ReactNode } from "react";
import { artifactApi, type ArtifactContent, type RuntimeArtifactLink } from "../api";
import { HtmlArtifactPreview } from "./HtmlArtifactPreview";

type LoadState = "loading" | "ready" | "empty" | "error";

async function saveArtifact(link: RuntimeArtifactLink): Promise<void> {
  const artifact = await artifactApi.download(link);
  const url = URL.createObjectURL(new Blob([artifact.content], { type: artifact.media_type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = artifact.filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function TextArtifactPreview({ link }: { link: RuntimeArtifactLink }) {
  const [artifact, setArtifact] = useState<ArtifactContent | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setArtifact(null);
    setError("");
    void artifactApi.read(link.artifact_id).then((value) => {
      if (active) setArtifact(value);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Artifact 加载失败");
    });
    return () => { active = false; };
  }, [link.artifact_id, link.link_id]);

  const download = artifact
    ? `data:${artifact.media_type};charset=utf-8,${encodeURIComponent(artifact.content)}`
    : "";

  return <section className="artifact-preview" aria-label="文本 Artifact 预览">
    <header><div><strong title={link.filename}>{link.filename}</strong><small>{link.media_type}</small></div>{artifact && <a href={download} download={link.filename} aria-label={`下载 ${link.filename}`}>下载</a>}</header>
    {error && <p role="alert">{error}</p>}
    {artifact && <pre>{artifact.content}</pre>}
  </section>;
}

export function RuntimeArtifactList({
  sessionId,
  refreshSignal,
  fallback,
}: {
  sessionId: string;
  refreshSignal: number;
  fallback: ReactNode;
}) {
  const [links, setLinks] = useState<RuntimeArtifactLink[]>([]);
  const [selectedLinkId, setSelectedLinkId] = useState("");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [manualRefresh, setManualRefresh] = useState(0);
  const requestGeneration = useRef(0);
  const linksRef = useRef<RuntimeArtifactLink[]>([]);

  useEffect(() => {
    let active = true;
    const generation = ++requestGeneration.current;
    setError("");
    setLoadState(linksRef.current.length ? "ready" : "loading");
    void artifactApi.list(sessionId).then((value) => {
      if (!active || requestGeneration.current !== generation) return;
      linksRef.current = value;
      setLinks(value);
      if (value.length === 0) {
        setSelectedLinkId("");
        setLoadState("empty");
        return;
      }
      const latest = value.reduce((candidate, item) => item.attempt >= candidate.attempt ? item : candidate);
      setSelectedLinkId((current) => value.some((item) => item.link_id === current) ? current : latest.link_id);
      setLoadState("ready");
    }).catch((reason: unknown) => {
      if (!active || requestGeneration.current !== generation) return;
      setError(reason instanceof Error ? reason.message : "Artifact 列表加载失败");
      setLoadState("error");
    });
    return () => { active = false; };
  }, [sessionId, refreshSignal, manualRefresh]);

  const selected = links.find((link) => link.link_id === selectedLinkId);
  const downloadHtml = async () => {
    if (!selected) return;
    try {
      await saveArtifact(selected);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Artifact 下载失败");
    }
  };

  return <section className="runtime-artifacts" aria-label="Runtime 发布产物">
    <header><h3>Runtime 发布产物</h3><button type="button" className="quiet" aria-label="刷新 Runtime 产物" onClick={() => setManualRefresh((value) => value + 1)}>刷新</button></header>
    {loadState === "loading" && <p aria-live="polite">正在读取当前会话产物…</p>}
    {loadState === "error" && <p role="alert">Runtime 产物暂不可用：{error}</p>}
    {loadState === "empty" && <section aria-label="示例产物"><strong>示例产物 · Fixture</strong>{fallback}</section>}
    {loadState === "ready" && <>
      <nav aria-label="Runtime Artifact 版本">
        {links.map((link) => <button key={link.link_id} type="button" className="quiet" aria-pressed={link.link_id === selectedLinkId} onClick={() => setSelectedLinkId(link.link_id)}>{link.filename} · Attempt {link.attempt}</button>)}
      </nav>
      {selected?.media_type === "text/html"
        ? <HtmlArtifactPreview artifactId={selected.artifact_id} filename={selected.filename} />
        : selected && ["text/plain", "text/markdown"].includes(selected.media_type)
          ? <TextArtifactPreview key={selected.link_id} link={selected} />
          : selected && <section className="artifact-preview"><p>该产物可下载，但当前格式不支持预览。</p><button type="button" className="quiet" aria-label={`下载 ${selected.filename}`} onClick={() => void downloadHtml()}>下载</button></section>}
    </>}
  </section>;
}

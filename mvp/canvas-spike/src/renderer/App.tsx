import { registry } from "./renderers";
import { artifacts } from "./artifacts";
import { useEffect, useState } from "react";
import { ProviderCenter } from "./providers/ProviderCenter";
import "./styles.css";

export function App() {
  const [view, setView] = useState(() => window.location.hash === "#providers" ? "providers" : "artifacts");
  useEffect(() => {
    const updateView = () => setView(window.location.hash === "#providers" ? "providers" : "artifacts");
    window.addEventListener("hashchange", updateView);
    return () => window.removeEventListener("hashchange", updateView);
  }, []);

  return (
    <main>
      <header className="app-header"><h1>Hermes Workbench</h1><nav aria-label="主导航"><a href="#artifacts" role="tab" aria-selected={view === "artifacts"} aria-current={view === "artifacts" ? "page" : undefined}>Artifacts</a><a href="#providers" aria-current={view === "providers" ? "page" : undefined}>模型供应商</a></nav></header>
      {view === "providers" ? <ProviderCenter /> : artifacts.map((artifact) => {
        const Renderer = registry.resolve(artifact.kind);
        return <Renderer key={artifact.id} artifact={artifact} />;
      })}
    </main>
  );
}

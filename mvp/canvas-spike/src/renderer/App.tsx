import { registry } from "./renderers";
import { artifacts } from "./artifacts";

export function App() {
  return (
    <main>
      <header><h1>Hermes Workbench</h1><button role="tab" aria-selected="true">Artifacts</button></header>
      {artifacts.map((artifact) => {
        const Renderer = registry.resolve(artifact.kind);
        return <Renderer key={artifact.id} artifact={artifact} />;
      })}
    </main>
  );
}

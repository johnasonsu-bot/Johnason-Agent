import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("workbenchBridge", {
  capabilities: (): string[] => ["artifact.read", "intervention.submit"],
  submitIntervention: (command: unknown): Promise<unknown> =>
    ipcRenderer.invoke("intervention.submit", command),
  apiRequest: (request: unknown): Promise<unknown> => ipcRenderer.invoke("api.request", request),
});

import { contextBridge } from "electron";

contextBridge.exposeInMainWorld("workbenchBridge", {
  capabilities: (): string[] => ["artifact.read"],
});


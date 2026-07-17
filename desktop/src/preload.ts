import { contextBridge, ipcRenderer } from "electron";

// Minimal, one-way bridge for the splash renderer only. The main window loads
// the real web app over http and needs no bridge.
contextBridge.exposeInMainWorld("vigil", {
  onStep: (cb: (data: { phase: string; status: string }) => void) =>
    ipcRenderer.on("step", (_e, data) => cb(data)),
  onLog: (cb: (line: string) => void) => ipcRenderer.on("log", (_e, line) => cb(line)),
  onError: (cb: (msg: string) => void) => ipcRenderer.on("error", (_e, msg) => cb(msg)),
  retry: () => ipcRenderer.invoke("retry"),
  quit: () => ipcRenderer.invoke("quit"),
});

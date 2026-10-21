import type { PluginHooks } from "../core/agent.js";
import type { PluginManifest } from "../contracts/plugin.js";
import { pluginFor } from "./registry.js";
import { tallyHooks } from "./tally/hooks.js";

// The code a plugin ships beside its manifest, by id; one missing here is files alone.
// Static imports, so hooks that do not compile fail the build, not a run.
const HOOKS: Readonly<Record<string, PluginHooks>> = {
  tally: tallyHooks,
};

export interface Plugin {
  manifest: PluginManifest;
  hooks?: PluginHooks;
}

export function loadPlugin(id: string, hooks: Readonly<Record<string, PluginHooks>> = HOOKS): Plugin {
  const manifest = pluginFor(id);
  const own = hooks[id];
  return own === undefined ? { manifest } : { manifest, hooks: own };
}

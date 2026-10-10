import type { PluginHooks } from "../core/agent.js";
import type { PluginManifest } from "../contracts/plugin.js";
import { pluginFor } from "./registry.js";
import { rootCauseHooks } from "./rootcause/hooks.js";
import { tallyHooks } from "./tally/hooks.js";

// The code a plugin ships beside its manifest, by plugin id. A plugin missing here
// is files alone. Imported statically, so a plugin whose hooks do not compile is a
// build error rather than a run that fails to load them.
const HOOKS: Readonly<Record<string, PluginHooks>> = {
  rootcause: rootCauseHooks,
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

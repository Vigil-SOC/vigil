import type { PluginManifest } from "../../contracts/plugin.js";

// A plugin that never asks a run to keep going: for tests about something else,
// whose scripts stop after a call or two. The ask has tests of its own.
export function quiet(plugin: PluginManifest): PluginManifest {
  const { min_results: _asked, ...rest } = plugin;
  return rest;
}

import { defineTool, type RegisteredTool, type ToolAdapter, type ToolBounds } from "../contracts/tool.js";
import { SpecError, type ToolSpec } from "../core/spec.js";

export const LOCAL = "local";

// A tool this process answers itself: remoteDispatch posts an id to a backend that
// has never seen the run's own record. The workflow that owns one supplies it.
export type LocalExecutor = ToolAdapter["execute"];

export function localTool(spec: ToolSpec, execute: LocalExecutor, bounds: ToolBounds): RegisteredTool {
  const description = typeof spec["description"] === "string" ? spec["description"] : "";
  if (description.trim() === "") throw new SpecError(`tool ${spec.id} needs a description; a model cannot choose between two blank tools`);

  const parameters = spec["parameters"];
  if (typeof parameters !== "object" || parameters === null) throw new SpecError(`tool ${spec.id} needs a parameters schema`);

  return defineTool({ id: spec.id, description, parameters: parameters as Record<string, unknown>, execute }, bounds, true);
}

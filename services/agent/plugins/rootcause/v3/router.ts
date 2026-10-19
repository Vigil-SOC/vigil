import type { FrontierDecision } from "./frontier.js";
import type { Graph, GraphPatch } from "./graph.js";
import { branch } from "./handlers/branch.js";
import { conclude } from "./handlers/conclude.js";
import { connect } from "./handlers/connect.js";
import { expand, fork } from "./handlers/expand.js";
import { pivot } from "./handlers/pivot.js";
import { strengthen } from "./handlers/strengthen.js";
import { test } from "./handlers/test.js";
import { theorize } from "./handlers/theorize.js";
import type { LoopContext } from "./types.js";

export function route(d: FrontierDecision, g: Readonly<Graph>, ctx: LoopContext): Promise<GraphPatch> {
  switch (d.move) {
    case "expand": return expand(d, g, ctx);
    case "fork": return fork(d, g, ctx);
    case "strengthen": return strengthen(d, g, ctx);
    case "pivot": return pivot(d, g, ctx);
    case "branch": return branch(d, g, ctx);
    case "theorize": return theorize(d, g, ctx);
    case "test": return test(d, g, ctx);
    case "connect": return connect(d, g, ctx);
    case "conclude": return conclude(d, g, ctx);
  }
}

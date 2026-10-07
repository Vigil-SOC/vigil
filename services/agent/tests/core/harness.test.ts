import { describe, expect, it } from "vitest";
import { harnessFor } from "../../harness.js";
import { InProcessState } from "../../core/state.js";
import { huntSpecFor } from "../support/hunt.js";

// The gateway bills nothing of its own, so pricing must be told who actually
// serves the model. Handed the literal "bifrost", the catalog guessed from the
// name and priced a paid "llama" on a commercial host at $0.
describe("the harness prices by the provider the spec names", () => {
  it("hands spec.provider to the provider surface", () => {
    const spec = { ...huntSpecFor(), model: "llama-3.3-70b-versatile", provider: "groq" };
    const harness = harnessFor("hunt", spec, new InProcessState());
    expect(harness.provider.provider_type).toBe("groq");
    expect(harness.provider.model).toBe("llama-3.3-70b-versatile");
  });

  it("falls back to the gateway when the spec names no provider", () => {
    const harness = harnessFor("hunt", huntSpecFor(), new InProcessState());
    expect(harness.provider.provider_type).toBe("bifrost");
  });
});

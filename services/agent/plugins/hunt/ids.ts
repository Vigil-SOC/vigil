import { randomBytes } from "node:crypto";

export function newId(prefix: string, bytes = 6): string {
  return `${prefix}-${randomBytes(bytes).toString("hex")}`;
}

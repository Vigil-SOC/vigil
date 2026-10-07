// The support-bundle command shown in the tray dialog. Deliberately free of
// `electron` imports so it can be exercised from a plain node script.
import * as path from "path";

// Double quotes keep spaces in one argument; escape what `sh` still expands inside them.
const shQuote = (s: string): string => `"${s.replace(/(["\\$`])/g, "\\$1")}"`;

// supportDir is the directory holding vigil-support.sh: <resourcesPath>/vigil-support
// when packaged, <checkout>/scripts/vigil-support from source.
export function supportCommand(supportDir: string): string {
  return `sh ${shQuote(path.join(supportDir, "vigil-support.sh"))}`;
}

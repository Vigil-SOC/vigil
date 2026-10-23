// Stage the resources the standalone (no-source-tree) stack mounts.
//
// The schema is applied by python -m core.upgrade inside the backend image,
// so this no longer extracts /app/database/init or writes 99_drop_seed_admin.sql.
const fs = require("fs");
const path = require("path");

const repoRoot = path.join(__dirname, "..", "..", "..");
const outDir = path.join(__dirname, "..", "standalone");

fs.mkdirSync(outDir, { recursive: true });

fs.copyFileSync(
  path.join(repoRoot, "infra", "docker", "bifrost", "config.json"),
  path.join(outDir, "bifrost-config.json"),
);

console.log("  • standalone: bifrost config");

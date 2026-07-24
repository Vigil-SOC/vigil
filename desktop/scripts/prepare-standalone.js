// Stage the resources the standalone (no-source-tree) stack mounts.
//
// The schema SQL is copied out of the pinned backend image rather than the
// working tree: a DMG built from a repo that has moved past the released tag
// would otherwise ship SQL the released backend doesn't expect. Extracting from
// the image makes the pair consistent by construction.
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const version = require("../package.json").version;
const image = `ghcr.io/vigil-soc/vigil-backend:${version}`;
const outDir = path.join(__dirname, "..", "standalone");
const dbInit = path.join(outDir, "db-init");

// stdio inherit/ignore yields a null stdout, so only trim a captured string.
const docker = (args, opts = {}) => {
  const out = execFileSync("docker", args, { encoding: "utf8", ...opts });
  return typeof out === "string" ? out.trim() : "";
};

fs.rmSync(dbInit, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

try {
  docker(["image", "inspect", image], { stdio: "ignore" });
} catch {
  console.log(`  • pulling ${image}`);
  docker(["pull", image], { stdio: "inherit" });
}

const cid = docker(["create", image]);
try {
  docker(["cp", `${cid}:/app/database/init/.`, dbInit]);
} finally {
  docker(["rm", "-f", cid], { stdio: "ignore" });
}

const sql = fs.readdirSync(dbInit).filter((f) => f.endsWith(".sql"));
if (!sql.length) throw new Error(`no schema SQL found in ${image}`);

// 06_auth_tables.sql seeds a default admin whose bcrypt hash matches no
// password, so the row cannot be signed into — it only stops the user table
// being empty, which is exactly the signal /api/auth/bootstrap uses to offer
// first-account creation. Drop it so the instance starts with no account and
// the user picks their own credentials. Sorts last, and keyed on the
// placeholder hash so it only ever removes the unusable seed: an admin the
// user created (or whose password they changed) is untouched, which matters
// because the seed re-runs on every launch.
fs.writeFileSync(
  path.join(dbInit, "99_drop_seed_admin.sql"),
  "DELETE FROM users WHERE username = 'admin' AND password_hash =\n" +
    "  '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aeWG6QErKLzG';\n",
);

fs.copyFileSync(
  path.join(__dirname, "..", "..", "docker", "bifrost", "config.json"),
  path.join(outDir, "bifrost-config.json"),
);

console.log(`  • standalone: ${sql.length} schema files from ${image}`);

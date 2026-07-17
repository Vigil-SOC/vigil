import { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog } from "electron";
import { spawn, execFileSync, ChildProcess } from "child_process";
import * as path from "path";
import * as fs from "fs";
import * as http from "http";
import * as crypto from "crypto";
import * as readline from "readline";

const BACKEND_URL = "http://127.0.0.1:6987";
const HEALTH_URL = `${BACKEND_URL}/api/health`;
const BACKEND_IMAGE = "ghcr.io/vigil-soc/vigil-backend";
const DOCKER_INSTALL_URL = "https://www.docker.com/products/docker-desktop/";
const OLLAMA_INSTALL_URL = "https://ollama.com/download";

// "source" drives the checkout's scripts/venv (development). "standalone" runs
// published images and needs nothing but Docker — that is what a downloaded DMG
// uses, since it has no repo around it.
type Mode = "source" | "standalone";

let mode: Mode | null = null;
let repoRoot: string | null = null;
let mainWindow: BrowserWindow | null = null;
let splashWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let stackReady = false;
let quitting = false;

const scriptsDir = () => path.join(repoRoot!, "scripts");

/* ---------------- standalone (image-based) stack ---------------- */

// Staged by scripts/prepare-standalone.js into extraResources: Docker reads
// these paths directly, and it cannot see inside app.asar.
const standaloneDir = () => path.join(process.resourcesPath, "standalone");
const composeFile = () => path.join(standaloneDir(), "docker-compose.yml");
const standaloneAvailable = () => fs.existsSync(composeFile());

const composeArgs = (...rest: string[]): string[] => [
  "compose",
  "-f",
  composeFile(),
  ...rest,
];

/* ---------------- config (survives across launches) ---------------- */

// Installed from the DMG, the app lives in /Applications and has no repo around
// it, so the chosen source folder is persisted here rather than re-asked.
const configPath = () => path.join(app.getPath("userData"), "config.json");

interface Config {
  repoRoot?: string;
  jwtSecret?: string;
}

function readConfig(): Config {
  try {
    return JSON.parse(fs.readFileSync(configPath(), "utf8"));
  } catch {
    return {};
  }
}

function writeConfig(patch: Config): void {
  try {
    const next = { ...readConfig(), ...patch };
    fs.mkdirSync(path.dirname(configPath()), { recursive: true });
    fs.writeFileSync(configPath(), JSON.stringify(next, null, 2), { mode: 0o600 });
  } catch (e) {
    console.error("could not persist config:", e);
  }
}

// The image defaults to DEV_MODE=false, where the backend fails closed unless
// JWT_SECRET_KEY is set. Mint one per install and keep it — a fresh secret each
// launch would invalidate every existing session.
function jwtSecret(): string {
  const saved = readConfig().jwtSecret;
  if (saved) return saved;
  const secret = crypto.randomBytes(48).toString("base64url");
  writeConfig({ jwtSecret: secret });
  return secret;
}

/* ---------------- locating the Vigil source tree ---------------- */

// "denied" matters as much as "missing": macOS TCC guards ~/Documents, ~/Desktop
// and ~/Downloads, and a GUI-launched app has no consent for them — existsSync
// just returns false, so a perfectly good checkout looks absent. Read the errno
// so we can say which it is instead of pretending the folder isn't there.
// (Launching from a terminal masks this: the app inherits the terminal's grant.)
type RepoProbe = "ok" | "missing" | "denied";

function probeRepo(dir: string): RepoProbe {
  try {
    fs.accessSync(path.join(dir, "VERSION"), fs.constants.R_OK);
    fs.accessSync(path.join(dir, "scripts", "app_up.sh"), fs.constants.R_OK);
    return "ok";
  } catch (e) {
    const code = (e as NodeJS.ErrnoException).code;
    return code === "EPERM" || code === "EACCES" ? "denied" : "missing";
  }
}

function hasRepoMarker(dir: string): boolean {
  return probeRepo(dir) === "ok";
}

// Set when a candidate checkout exists but macOS won't let us read it, so the
// prompt can explain the permission rather than claim the folder is missing.
let deniedPath: string | null = null;

// Conventional clone locations, probed relative to $HOME so the bundle carries
// no absolute path from the build machine.
const COMMON_CLONE_DIRS = [
  "Documents/GitHub/vigil",
  "Documents/vigil",
  "GitHub/vigil",
  "Developer/vigil",
  "Projects/vigil",
  "src/vigil",
  "code/vigil",
  "vigil",
];

// Order: env override → saved choice → walk up from __dirname (dev / .app still
// inside the repo) → conventional clone locations under $HOME (an installed .app
// can't walk up to the repo). Null means "ask the user".
function findRepoRoot(): string | null {
  deniedPath = null;
  const candidates: string[] = [];

  const env = process.env.VIGIL_REPO_ROOT;
  if (env) candidates.push(path.resolve(env));

  const saved = readConfig().repoRoot;
  if (saved) candidates.push(saved);

  let dir = __dirname;
  for (let i = 0; i < 10; i++) {
    candidates.push(dir);
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  const home = app.getPath("home");
  for (const rel of COMMON_CLONE_DIRS) candidates.push(path.join(home, rel));

  for (const c of candidates) {
    const probe = probeRepo(c);
    if (probe === "ok") return c;
    if (probe === "denied" && !deniedPath) deniedPath = c;
  }
  return null;
}

// Deep link to Privacy & Security → Full Disk Access.
const FDA_SETTINGS_URL =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles";

async function promptForRepoRoot(): Promise<string | null> {
  const denied = deniedPath !== null;

  // Denied means the checkout is there but TCC-gated (Documents/Desktop/…).
  // Vigil isn't sandboxed, so an open-panel pick grants access for this run but
  // doesn't persist — only a Full Disk Access grant survives a relaunch. Lead
  // with that, and keep the pick as the "just let me in now" escape hatch.
  const buttons = denied
    ? ["Open Privacy Settings", "Choose Folder…", "Quit"]
    : ["Choose Folder…", "Quit"];

  const { response } = await dialog.showMessageBox({
    type: denied ? "warning" : "info",
    title: denied ? "Vigil needs permission" : "Locate Vigil",
    message: denied
      ? "macOS is blocking access to your Vigil folder"
      : "Select your Vigil source folder",
    detail: denied
      ? `Your checkout is at ${deniedPath}, but macOS protects Documents, ` +
        `Desktop and Downloads and won't let Vigil read it.\n\n` +
        `Recommended: give Vigil Full Disk Access, then reopen it — this is a ` +
        `one-time setup that sticks.\n\n` +
        `Or choose the folder to continue right now (macOS may ask again next launch).`
      : "Vigil runs its services from a local clone of the repository. " +
        "Choose the folder you cloned it into (it contains VERSION and scripts/).",
    buttons,
    defaultId: 0,
    cancelId: buttons.length - 1,
  });

  if (denied && response === 0) {
    await shell.openExternal(FDA_SETTINGS_URL);
    await dialog.showMessageBox({
      type: "info",
      title: "After granting access",
      message: "Add Vigil to Full Disk Access, then reopen it.",
      detail:
        "In the window that just opened, enable Vigil (use + to add it from " +
        "/Applications if it isn't listed). macOS requires a relaunch for the " +
        "new permission to take effect.",
      buttons: ["Quit Vigil"],
    });
    return null;
  }
  if (response !== (denied ? 1 : 0)) return null;

  const picked = await dialog.showOpenDialog({
    properties: ["openDirectory"],
    buttonLabel: "Use This Folder",
    defaultPath: deniedPath ?? app.getPath("home"),
  });
  const dir = picked.filePaths[0];
  if (picked.canceled || !dir) return null;

  if (!hasRepoMarker(dir)) {
    await dialog.showMessageBox({
      type: "error",
      title: "Not a Vigil folder",
      message: "That folder doesn't look like a Vigil checkout.",
      detail: "Expected to find VERSION and scripts/app_up.sh inside it.",
    });
    return promptForRepoRoot();
  }
  writeConfig({ repoRoot: dir });
  return dir;
}

/* ---------------- external app dependencies ---------------- */

// Vigil drives apps the user installs rather than bundling them: Docker runs
// postgres/redis/bifrost, Ollama runs local models natively (Docker on macOS
// has no Metal passthrough). Report what's missing before the stack fails.
function whichBin(bin: string): string | null {
  try {
    return execFileSync("/usr/bin/which", [bin], { env: augmentedEnv() })
      .toString()
      .trim() || null;
  } catch {
    return null;
  }
}

async function checkDependencies(): Promise<boolean> {
  if (!whichBin("docker")) {
    const { response } = await dialog.showMessageBox({
      type: "error",
      title: "Docker is required",
      message: "Docker Desktop isn't installed.",
      detail:
        "Vigil runs PostgreSQL, Redis and the Bifrost LLM gateway as Docker " +
        "containers. Install Docker Desktop, then reopen Vigil.",
      buttons: ["Get Docker Desktop", "Quit"],
      defaultId: 0,
      cancelId: 1,
    });
    if (response === 0) await shell.openExternal(DOCKER_INSTALL_URL);
    return false;
  }
  // Ollama is optional — only local models need it, so note it and carry on.
  if (!whichBin("ollama")) {
    sendSplash("log", `Ollama not installed (optional) — see ${OLLAMA_INSTALL_URL}`);
  }
  return true;
}

// A GUI-launched app inherits launchd's minimal PATH, not the one from your
// shell profile — so python (python.org), node (nvm/conda) and docker are all
// invisible to the scripts we spawn. Ask the login shell for its real PATH
// rather than guessing install locations; that's the only way to match what the
// user sees in their terminal. Falls back to a common-paths list if the shell
// can't be queried. Resolved once — spawning a login shell isn't free.
let cachedPath: string | null = null;

function loginShellPath(): string {
  if (cachedPath) return cachedPath;
  const fallback = [
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    path.join(app.getPath("home"), ".local", "bin"),
    "/Applications/Docker.app/Contents/Resources/bin",
  ].join(":");

  if (process.platform === "win32") return (cachedPath = process.env.PATH || "");
  const shell = process.env.SHELL || "/bin/zsh";
  try {
    const out = execFileSync(shell, ["-ilc", "printf %s \"$PATH\""], {
      encoding: "utf8",
      timeout: 5000,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    cachedPath = out ? `${out}:${fallback}` : fallback;
  } catch {
    cachedPath = fallback;
  }
  return cachedPath;
}

function augmentedEnv(): NodeJS.ProcessEnv {
  return { ...process.env, PATH: loginShellPath() };
}

function sendSplash(channel: string, payload: unknown): void {
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.webContents.send(channel, payload);
  }
}

// True when the source tree hasn't been prepared yet (first run / after clone):
// no venv, frontend deps missing, or the SPA the window loads isn't built.
function needsSetup(): boolean {
  return (
    !fs.existsSync(path.join(repoRoot!, "venv", "bin", "python")) ||
    !fs.existsSync(path.join(repoRoot!, "frontend", "node_modules")) ||
    !fs.existsSync(path.join(repoRoot!, "frontend", "build", "index.html"))
  );
}

function pingHealth(timeoutMs = 2000): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(HEALTH_URL, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

// Run one orchestration script, forwarding its `STEP <phase> <status>` stdout
// lines to the splash. Resolves with an exit code; never rejects.
//
// `doneWhen` lets a script that launches a long-lived daemon be considered
// finished on its terminal STEP line rather than on process close — app_up.sh
// leaves the backend running, so waiting for `close` would hang.
function runScript(name: string, args: string[] = [], doneWhen?: string): Promise<number> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (code: number) => {
      if (settled) return;
      settled = true;
      resolve(code);
    };
    const proc: ChildProcess = spawn("bash", [path.join(scriptsDir(), name), ...args], {
      cwd: repoRoot!,
      env: augmentedEnv(),
    });
    const out = readline.createInterface({ input: proc.stdout! });
    out.on("line", (line) => {
      const m = line.match(/^STEP (\S+) (\S+)$/);
      if (m) {
        sendSplash("step", { phase: m[1], status: m[2] });
        if (m[2] === "fail") finish(1);
        if (doneWhen && m[1] === doneWhen && m[2] === "done") finish(0);
      }
      const u = line.match(/^URL (\S+)$/);
      if (u) sendSplash("log", `Ready at ${u[1]}`);
    });
    const err = readline.createInterface({ input: proc.stderr! });
    err.on("line", (line) => sendSplash("log", line));
    proc.on("close", (code) => finish(code ?? 1));
    proc.on("error", (e) => {
      sendSplash("log", `Failed to run ${name}: ${e.message}`);
      finish(1);
    });
  });
}

// VIGIL_VERSION pins the backend image to this app's version so the two can
// never drift apart.
const spawnCompose = (args: string[]) =>
  spawn("docker", args, {
    cwd: standaloneDir(),
    env: { ...augmentedEnv(), VIGIL_VERSION: app.getVersion(), JWT_SECRET_KEY: jwtSecret() },
  });

// Run `docker compose …`, streaming progress to the splash.
function runCompose(args: string[]): Promise<number> {
  return new Promise((resolve) => {
    const proc = spawnCompose(args);
    const relay = (s: NodeJS.ReadableStream) =>
      readline.createInterface({ input: s }).on("line", (l) => {
        if (l.trim()) sendSplash("log", l);
      });
    relay(proc.stdout!);
    relay(proc.stderr!);
    proc.on("close", (code) => resolve(code ?? 1));
    proc.on("error", (e) => {
      sendSplash("log", `docker compose failed: ${e.message}`);
      resolve(1);
    });
  });
}

// Stop the stack the way it was started. `keepDocker` leaves containers up for
// a faster restart; in standalone the containers ARE the stack, so it stops
// them either way and only `down` (on quit) removes them.
async function stopStack(keepDocker = false): Promise<void> {
  if (mode === "standalone") await runCompose(composeArgs("stop"));
  else await runScript("app_down.sh", keepDocker ? [] : ["--stop-docker"]);
}

// Standalone has no logs/ directory on disk — the logs live in the containers,
// so snapshot them to a file the OS can open.
async function openLogs(): Promise<void> {
  if (mode !== "standalone") return void shell.openPath(path.join(repoRoot!, "logs"));
  const file = path.join(app.getPath("temp"), "vigil-logs.txt");
  const text = await new Promise<string>((resolve) => {
    const proc = spawnCompose(composeArgs("logs", "--tail", "500", "--no-color"));
    let out = "";
    proc.stdout!.on("data", (d) => (out += d));
    proc.stderr!.on("data", (d) => (out += d));
    proc.on("close", () => resolve(out));
    proc.on("error", (e) => resolve(String(e)));
  });
  fs.writeFileSync(file, text || "No container logs yet.");
  await shell.openPath(file);
}

// The pinned backend image is the large one and the one that changes per
// release, so its presence stands in for "already downloaded". Any other
// missing image is pulled by `up` anyway.
function backendImagePresent(): Promise<boolean> {
  return new Promise((resolve) => {
    const proc = spawn("docker", ["image", "inspect", `${BACKEND_IMAGE}:${app.getVersion()}`], {
      env: augmentedEnv(),
      stdio: "ignore",
    });
    proc.on("close", (code) => resolve(code === 0));
    proc.on("error", () => resolve(false));
  });
}

async function bringUpStandalone(): Promise<boolean> {
  const phase = (p: string, status: string) => sendSplash("step", { phase: p, status });

  // Pulled separately from `up` so first run reports the multi-GB download as
  // its own step instead of looking hung. Skipped once the image is local, so a
  // normal launch neither stalls nor needs the network.
  if (!(await backendImagePresent())) {
    phase("images", "start");
    if ((await runCompose(composeArgs("pull"))) !== 0) {
      phase("images", "fail");
      sendSplash("error", "Could not download the Vigil images. Check your connection.");
      return false;
    }
    phase("images", "ok");
  }

  phase("services", "start");
  if ((await runCompose(composeArgs("up", "-d", "--wait"))) !== 0) {
    phase("services", "fail");
    sendSplash("error", "The Vigil containers did not start. See the log above.");
    return false;
  }
  phase("services", "ok");

  // Only now does the schema exist (the backend runs create_all at startup), so
  // the seed data — roles, the default admin, SLA policies — can land.
  phase("schema", "start");
  if ((await runCompose(composeArgs("run", "--rm", "--no-deps", "db-seed"))) !== 0) {
    phase("schema", "fail");
    sendSplash("error", "Could not prepare the database. See the log above.");
    return false;
  }
  phase("schema", "ok");
  return true;
}

function createSplash(): void {
  splashWindow = new BrowserWindow({
    width: 520,
    height: 380,
    resizable: false,
    frame: false,
    show: true,
    backgroundColor: "#0c0f14",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  splashWindow.loadFile(path.join(__dirname, "..", "src", "splash.html"));
}

function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    backgroundColor: "#0c0f14",
    title: "Vigil",
    // macOS only: hide the title bar so content runs edge-to-edge, with the
    // traffic lights floating over the nav rail. Every other platform keeps its
    // native frame — without an equivalent overlay, a frameless window would
    // have no close/minimize controls at all (the web UI provides none).
    ...(process.platform === "darwin"
      ? { titleBarStyle: "hiddenInset" as const, trafficLightPosition: { x: 8, y: 13 } }
      : {}),
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  mainWindow.loadURL(BACKEND_URL);
  // With the OS title bar hidden, the traffic lights would overlap the nav-rail
  // logo. Rather than resize the console (position:fixed; inset:0) — which
  // throws off every child sized with calc(100vh - X) and clips bottom content
  // like the rail user card — keep it full height and just: pad the rail top to
  // clear the lights, and make the rail + topbar draggable (Claude-style),
  // excluding their interactive elements so buttons still click. macOS only.
  if (process.platform === "darwin") {
    const TITLEBAR_CSS = `
      .rail { padding-top: 42px !important; }
      .rail, .topbar { -webkit-app-region: drag; }
      .rail button, .rail a, .rail input, .rail [role="button"],
      .topbar button, .topbar a, .topbar input, .topbar select,
      .topbar [role="button"], .topbar [tabindex] { -webkit-app-region: no-drag; }
    `;
    mainWindow.webContents.on("dom-ready", () => {
      mainWindow?.webContents.insertCSS(TITLEBAR_CSS);
    });
  }
  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
    splashWindow = null;
  });
  // Open external links (docs, integrations) in the system browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(BACKEND_URL)) {
      shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });
  // Closing the window means "I'm done" — quit the app, which runs the full
  // shutdown (before-quit). The `quitting` guard avoids recursing when quit
  // itself closes the window.
  mainWindow.on("close", () => {
    if (!quitting) app.quit();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

async function bringUpSource(): Promise<boolean> {
  if (needsSetup()) {
    sendSplash("log", "First run: setting up dependencies (this can take a few minutes)…");
    if ((await runScript("app_setup.sh")) !== 0) {
      sendSplash("error", "Setup failed. See the log above.");
      return false;
    }
  }
  if ((await runScript("app_up.sh", [], "up")) !== 0) {
    sendSplash("error", "Could not start the Vigil stack. See the log above.");
    return false;
  }
  return true;
}

async function bringUpStack(): Promise<void> {
  stackReady = false;
  updateTray();
  // Something already serving :6987 (a dev stack, or a previous run) would make
  // our own bind fail; adopt it instead of fighting over the port.
  if (await pingHealth()) {
    sendSplash("log", "Vigil is already running — connecting.");
    stackReady = true;
    updateTray();
    createMainWindow();
    return;
  }
  const ok = mode === "standalone" ? await bringUpStandalone() : await bringUpSource();
  if (!ok) return;
  // app_up.sh already waited on health; confirm before swapping windows.
  for (let i = 0; i < 30; i++) {
    if (await pingHealth()) {
      stackReady = true;
      updateTray();
      createMainWindow();
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  sendSplash("error", "Backend did not become reachable.");
}

function trayTemplate(): Electron.MenuItemConstructorOptions[] {
  return [
    { label: stackReady ? "Vigil — running" : "Vigil — starting…", enabled: false },
    { type: "separator" },
    {
      label: "Show Window",
      click: () => {
        if (mainWindow) mainWindow.show();
        else if (stackReady) createMainWindow();
      },
    },
    { label: "Open in Browser", enabled: stackReady, click: () => shell.openExternal(BACKEND_URL) },
    { type: "separator" },
    {
      label: "Restart Stack",
      click: async () => {
        // Leave containers up across a restart — faster, and bring-up reuses them.
        await stopStack(true);
        if (!splashWindow) createSplash();
        await bringUpStack();
      },
    },
    {
      label: "Stop Stack",
      enabled: stackReady,
      click: async () => {
        await stopStack();
        stackReady = false;
        updateTray();
      },
    },
    { label: "Open Logs", click: () => openLogs() },
    { type: "separator" },
    { label: "Quit Vigil", click: () => app.quit() },
  ];
}

function updateTray(): void {
  if (tray) tray.setContextMenu(Menu.buildFromTemplate(trayTemplate()));
}

// The tray is a convenience, never a dependency: Linux needs libappindicator /
// StatusNotifier and GNOME dropped tray support outright, so Tray construction
// can legitimately throw there. Failing here must not stop the stack from
// starting, so swallow it and carry on windowed.
function createTray(): void {
  const iconPath = path.join(__dirname, "..", "build", "icons", "tray.png");
  if (!fs.existsSync(iconPath)) return;
  try {
    const img = nativeImage.createFromPath(iconPath);
    if (img.isEmpty()) return;
    if (process.platform === "darwin") img.setTemplateImage(true);
    tray = new Tray(img);
    tray.setToolTip("Vigil");
    updateTray();
  } catch (e) {
    tray = null;
    console.error("tray unavailable on this desktop:", e);
  }
}

ipcMain.handle("retry", async () => {
  await bringUpStack();
});

ipcMain.handle("quit", () => app.quit());

app.whenReady().then(async () => {
  // Order matters: the external apps we drive must exist, and we must know
  // where the source tree is, before any script can run.
  if (!(await checkDependencies())) return app.exit(1);

  // A checkout wins when present: its live source is the point of running from
  // one. Otherwise fall back to images, and only ask for a folder as a last
  // resort — an installed copy has no repo and needs no prompt. VIGIL_MODE
  // overrides the choice, which is the only way to exercise standalone on a
  // machine that has a checkout.
  repoRoot = process.env.VIGIL_MODE === "standalone" ? null : findRepoRoot();
  if (!repoRoot && standaloneAvailable()) {
    mode = "standalone";
  } else {
    repoRoot = repoRoot ?? (await promptForRepoRoot());
    if (!repoRoot) return app.exit(1);
    mode = "source";
  }

  createSplash();
  createTray();
  bringUpStack();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (stackReady) createMainWindow();
      else createSplash();
    }
  });
});

// Full shutdown on quit: stop the backend AND the Docker containers the app
// started ("close it and it shuts everything down"). Ollama is left alone — it
// may be the user's own instance; Docker Desktop itself stays running. Hold the
// quit until app_down.sh returns so nothing is orphaned.
app.on("before-quit", async (e) => {
  if (quitting || !mode) return;
  e.preventDefault();
  quitting = true;
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();
  try {
    // `down` without -v: containers go, the volumes holding cases and
    // credentials stay.
    if (mode === "standalone") await runCompose(composeArgs("down"));
    else await runScript("app_down.sh", ["--stop-docker"]);
  } catch {
    /* best effort */
  }
  app.exit(0);
});

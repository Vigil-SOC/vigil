// Ad-hoc sign the macOS bundle (afterPack, so the DMG wraps a signed app).
//
// electron-builder's `identity: null` skips signing altogether, which leaves the
// bundle carrying only Electron's linker signature: it identifies as "Electron",
// doesn't cover our Info.plist or resources, and fails `codesign --verify`. macOS
// then can't identify the app, so TCC silently denies protected folders
// (Documents/Desktop/…) without ever showing a permission prompt.
//
// An ad-hoc signature isn't a Developer ID — a downloaded copy still needs
// notarization to pass Gatekeeper cleanly — but it makes the bundle internally
// valid and identifiable, which is what the permission prompts key off.
const { execFileSync } = require("child_process");
const path = require("path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;

  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${appName}.app`);
  const appId = context.packager.appInfo.id;

  execFileSync(
    "codesign",
    ["--force", "--deep", "--sign", "-", "--identifier", appId, appPath],
    { stdio: "inherit" },
  );
  execFileSync("codesign", ["--verify", "--strict", appPath], { stdio: "inherit" });
  console.log(`  • ad-hoc signed ${appName}.app as ${appId}`);
};

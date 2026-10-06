# vigil-support

`vigil-support.sh` writes one redacted `tar.gz` that a person can attach to a
support request. It is POSIX `sh` using baseline tools only (no `jq`, no Vigil
Python), and runs on Linux and macOS. This file is the record of the bundle
format.

```
sh scripts/vigil-support/vigil-support.sh [--mode native|compose|desktop] [--state-dir DIR] [--since DAYS]
```

| Option | Meaning |
| --- | --- |
| `--mode` | Only look for that kind of install. `native` and `compose` mean a checkout, `desktop` means Desktop standalone. |
| `--state-dir` | Vigil's State Directory. Default `$VIGIL_DIR`, else `~/.vigil` (Desktop: its app-data directory). |
| `--since DAYS` | How far back journal and macOS log sources reach. Default 7. macOS `log show` uses 24 h unless `--since` is given. |

The bundle is written to the current directory as
`vigil-support-<install|host>-<version>-<UTC yyyymmddThhmmssZ>.tar.gz`
(`install` when one install was found, `host` otherwise). If that name is taken
by a concurrent run, `-1`, `-2`, ... is appended. An earlier bundle is never
collected into a new one.

## Detection

- **Native and Compose from source**: a Vigil checkout (the one holding the
  script, `$VIGIL_REPO_ROOT`, or the current directory) plus running
  `deeptempo-*` containers. `compose` when `deeptempo-backend` runs, else `native`.
- **Desktop standalone**: running containers of Compose project `vigil`.
- More than one install found: nothing is written, the installs are listed, and
  the exit code is 1. Pick one with `--mode`.
- None found: a host-only bundle that says so and records, in `manifest.json`
  under `looked`, where the script looked.

## Layout

```
SUMMARY.txt
manifest.json
configuration/
health/
logs/
system/
```

`configuration/`, `health/` and `logs/` hold the install's own files (below);
with no install found each is an empty directory with a `not collected` entry.
`system/` holds the host-level files in the second table.

| Path | Source |
| --- | --- |
| `configuration/env` | the checkout's `.env` (native, compose) |
| `configuration/compose-config.yml` | `docker compose -p <project> -f <files> config` (compose, desktop). The project and files come from the `com.docker.compose.project` and `...config_files` container labels, else the checkout's `infra/docker/docker-compose.yml` or the Desktop's staged copy. If interpolation fails (the Desktop injects its tokens at launch) the uninterpolated file is used. |
| `configuration/state/` | `backups.json` (State Directory), `mcp-config.json`, `INTENT.md`, `.vigil-autostart` (checkout, else State Directory) |
| `configuration/deployment/` | native, compose: `infra/docker/` compose, OpenTelemetry, Prometheus and Bifrost config plus Grafana provisioning YAML. Desktop: its staged compose file. |
| `configuration/never-included/` | see below |
| `health/*.json`, `health/*.txt` | `curl` of the API `/api/health` (`VIGIL_API_URL`), daemon `:9091/health` and `/status`, webhook receiver `:8081/health`, agent `:6989` and `:6990` `/healthz`, Bifrost `:8080/health`, stored as returned. An endpoint that does not answer is `not collected` with curl's message. |
| `health/containers.txt` | `docker inspect --format` per container: state, restart count, start time, image. Never raw inspect, never container environments. |
| `logs/checkout/` | native, compose: everything under the checkout's `logs/`, including `.1`-`.4` rotations, PID files and `containers/` |
| `logs/state/vigil.log` | native, compose: `vigil.log` in the State Directory |
| `logs/docker/<container>.log` | compose, desktop: `docker logs --timestamps --since <DAYS> days` |
| `logs/desktop/` | desktop: the app log directory (`vigil-desktop.log*` and the `containers/vigil-*.log` snapshots written on quit). Linux `<State Directory>/logs` (`~/.config/Vigil/logs`), macOS `~/Library/Logs/Vigil`. |

Containers are listed from `docker ps -a`: names `deeptempo-*` or the install's
Compose project (native, compose), project `vigil` (desktop). Lab and demo
containers (`deeptempo-splunk`, `-kafka`, `-elasticsearch`, `-kibana`,
`-misp-*`, `-pgadmin`) and any Ollama container are `not collected` with the
reason `excluded: lab/demo container; state <running state>`. A container that
cannot be read is `not collected` with docker's message. Native installs take
no `docker logs`: `start.sh` saves those under the checkout's `logs/`.

Under Compose and Desktop the State Directory lives in a container volume, which
is not read; only files on the host are.

### Never included

Recorded as `configuration/never-included/<name>`, `not collected`, reason
`never included; present` or `never included; absent`, source the path. The
content is not read: `secrets.enc`, `master.key`, `jwt_secret`, the State
Directory `.env`, `~/.deeptempo/.env`, the Desktop app's `config.json`, the
State Directory's `bifrost/` data, and each local backup repository named by
`backups.json` (a remote repository is noted as not checked). Passphrases live
in `secrets.enc`.

### System

| File | Source |
| --- | --- |
| `host.txt` | `hostname`, `uname -a` |
| `clock.txt` | `date` in UTC and local time, timezone abbreviation |
| `timezone-sync.txt` | `timedatectl` (Linux) or `systemsetup` (macOS) |
| `processes.txt` | `ps -ef`, the full list |
| `processes-vigil.txt` | the same list filtered to `vigil` and `deeptempo` |
| `disk.txt` | `df -Pk` for Vigil's write locations that exist |
| `os-release.txt` | `/etc/os-release` or `sw_vers` |
| `journal.txt` | Linux: `journalctl --since "<DAYS> days ago"` |
| `syslog.txt` | Linux: `/var/log/syslog`, else `/var/log/messages` |
| `kernel.txt` | Linux: `dmesg` |
| `macos-log.txt` | macOS: `log show`, Docker and Vigil entries only |

Logs a normal user cannot read are `not collected` with the reason
`needs elevation`; the final output says that running with `sudo` includes them.

## `manifest.json`

```json
{
  "format_version": 1,
  "support_version": "0.6.0",
  "created_utc": "2026-10-05T19:14:56Z",
  "mode": "host",
  "host_os": "Linux 6.12.94",
  "install": {"found": false, "description": "no Vigil install found", "api_version": null},
  "looked": ["checkout: none at /tmp", "docker: not installed"],
  "entries": [
    {"path": "system/processes.txt", "state": "collected", "reason": "ok",
     "source": "ps -ef", "bytes": 24326, "redactions": 5},
    {"path": "system/kernel.txt", "state": "not collected", "reason": "needs elevation",
     "source": "dmesg"}
  ]
}
```

Every item appears exactly once with a `state` and a free-text `reason`.

- `state` is `collected` or `not collected`.
- A `collected` entry has `bytes` (as stored) and `redactions` (replacements by
  the filter). A source cut at its size ceiling is still `collected` and
  carries `bytes_cut`, the number of oldest bytes dropped.
- `install.api_version` is the `version` from `/api/health` when it answered
  (`VIGIL_API_URL`, default `http://localhost:6987`), else `null`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | A bundle was written. Not-collected items are listed in the final output. |
| 1 | No bundle was written: several installs found, not enough free space, a missing redaction filter, bad options, or a write failure. |

## `SUMMARY.txt`

A short plain list: version, mode, host OS, UTC time; a line when the tool's
`VERSION` differs from the version `/api/health` reports; the not-collected
items with reasons; per-file redaction counts; the sentence that credentials in
free log text matching no known format cannot be guaranteed caught; and the data
notice.

## Redaction

Every captured byte goes through `redact.awk` with `secret-names.txt`
(`awk -f redact.awk -v names=... -v values=... -v counts=... -v name=<file>`)
before it enters the bundle. The `.env` and the rendered Compose config are read
first: each value the filter redacts by name there (6+ characters) goes into a
file in the private work directory and is passed as `values`, so the same value
is also replaced in free text such as container logs and the process list. The
values never reach the bundle, the manifest or the output. A not-collected
reason that quotes a command's error message goes through the filter too. If the filter cannot run, the item is `not collected`; the
unredacted input is never copied. `SUMMARY.txt` and `manifest.json` are written
by the script itself and hold only paths, versions and reasons.

## Safety

- `umask 077` throughout; the bundle is mode 0600 and holds plain text only.
- Work happens in a private temp directory that is removed on exit, INT and
  TERM, along with any helper processes. A reserved output name is removed too.
- Free space is checked first (1 GiB on the temp and output file systems).
  Nothing is written when it is short.
- The script never reads stdin and never prompts. `ENABLE_KEYRING` is unset,
  and nothing it runs touches the keychain or changes the install.
- Limits: 60 s per source (90 s for system logs), 5 min overall, 50 MiB raw per
  source keeping the newest bytes, 1 GiB raw per bundle. The time limit is plain
  `sh` (macOS has no `timeout`).
- `LC_ALL=C`. A symlink is followed only to a regular file inside the location
  being collected; otherwise it is recorded as `not collected`.

## Data notice

Printed at the start and end of every run, and ending `SUMMARY.txt`:

> DATA NOTICE: this bundle holds information from this machine: its hostname,
> the full process list with command lines, system logs and disk usage. Known
> credential formats and secret names are redacted, but credentials in free log
> text that match no known format cannot be guaranteed caught. Nothing is
> uploaded. Review the bundle before you share it.

The end of the run then prints the bundle path, size and SHA-256 (`sha256sum`
or `shasum -a 256`), then the not-collected items.

## `VERSION`

`VERSION` ships beside the script and follows the Vigil release (release-please
bumps it). Its first word is the version; the rest of the line is ignored.

## Tests

`tests/unit/test_support_bundle.py` drives the script with stub tools on
`PATH`. It reads these overrides, which are not for normal use:
`VIGIL_SUPPORT_SOURCE_SECS`, `VIGIL_SUPPORT_LOG_SECS`, `VIGIL_SUPPORT_TOTAL_SECS`,
`VIGIL_SUPPORT_SOURCE_MAX`, `VIGIL_SUPPORT_NOW` (the timestamp in the name) and
`VIGIL_SUPPORT_FS_ROOT` (a prefix for `/etc` and `/var/log` paths).

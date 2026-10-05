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

`configuration/`, `health/` and `logs/` hold per-install collection, which this
version does not do: each is an empty directory with a `not collected` entry in
the manifest. `system/` holds the host-level files below.

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
(`awk -f redact.awk -v names=... -v counts=... -v name=<file>`) before it enters
the bundle. If the filter cannot run, the item is `not collected`; the
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

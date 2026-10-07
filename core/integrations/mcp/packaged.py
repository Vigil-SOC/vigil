"""Pinned npx/uvx specs in mcp-config.json and the copies the backend image bakes.

The backend image installs every pinned npx package under ``NPM_PREFIX`` and
every uvx tool under ``UV_TOOL_DIR`` at build time. ``installed_launch`` swaps an
entry's ``npx``/``uvx`` command for that copy when its exact pin is installed,
and returns the declared command otherwise (local dev, or a pin the image lacks).
"""

from __future__ import annotations

import json
import re
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import List, Optional, Tuple

NPM_PREFIX = Path("/opt/vigil/mcp/npm")
UV_TOOL_DIR = Path("/opt/vigil/mcp/uv-tools")

# uvx flags whose next token is a package spec.
UVX_SPEC_FLAGS = {"--from", "--with"}
# uvx flags whose next token is not a package (interpreter, path, index).
UVX_VALUE_FLAGS = {
    "--python",
    "--with-editable",
    "--with-requirements",
    "--index",
    "--extra-index-url",
}


def npm_version(spec: str) -> Optional[str]:
    """Return the version suffix of an npm package spec, or None if missing."""
    rest = spec[1:] if spec.startswith("@") else spec
    if "@" not in rest:
        return None
    return rest.rsplit("@", 1)[1]


def npm_name(spec: str) -> str:
    version = npm_version(spec)
    return spec[: -len(version) - 1] if version is not None else spec


def npx_package(args: List[str]) -> Tuple[Optional[str], List[str]]:
    """Return the package spec npx runs and the args after it."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-p", "--package") and i + 1 < len(args):
            return args[i + 1], args[i + 2 :]
        if arg.startswith("-"):
            i += 1
            continue
        return arg, args[i + 1 :]
    return None, []


def pypi_name(spec: str) -> str:
    return re.split(r"[=<>!~\[;@ ]", spec, maxsplit=1)[0]


def uvx_specs(
    args: List[str],
) -> Tuple[Optional[str], List[str], Optional[str], List[str]]:
    """Split a uvx argv into (package spec, --with specs, executable, args after it).

    The package is the ``--from`` spec, else the first positional; the executable
    is the positional, which without ``--from`` is named after the package.
    """
    from_spec: Optional[str] = None
    with_specs: List[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in UVX_SPEC_FLAGS and i + 1 < len(args):
            if arg == "--from":
                from_spec = args[i + 1]
            else:
                with_specs.append(args[i + 1])
            i += 2
            continue
        if arg in UVX_VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        if from_spec is not None:
            return from_spec, with_specs, arg, args[i + 1 :]
        exe = None if "git+" in arg else pypi_name(arg)
        return arg, with_specs, exe, args[i + 1 :]
    return from_spec, with_specs, None, []


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _git_url_ref(spec: str) -> Tuple[str, str]:
    url, _, ref = spec.split("git+", 1)[1].rpartition("@")
    return url.removesuffix(".git"), ref


def _spec_installed(spec: str, dists: List[Distribution]) -> bool:
    """True when a ``name==version`` or ``git+url@ref`` spec is what is installed."""
    if "git+" in spec:
        url, ref = _git_url_ref(spec)
        for dist in dists:
            try:
                direct = json.loads(dist.read_text("direct_url.json") or "{}")
            except ValueError:
                continue
            vcs = direct.get("vcs_info") or {}
            if (
                direct.get("url", "").removesuffix(".git") == url
                and vcs.get("requested_revision") == ref
            ):
                return True
        return False
    name, _, version = spec.partition("==")
    return any(
        _canonical(dist.metadata["Name"] or "") == _canonical(name)
        and dist.version == version
        for dist in dists
    )


def installed_npx(args: List[str]) -> Optional[List[str]]:
    """``node <bin> <args>`` for an npx entry whose exact pin is under ``NPM_PREFIX``."""
    spec, rest = npx_package(args)
    if not spec or npm_version(spec) is None:
        return None
    # `npx -p pkg cmd` names its command separately; only `npx pkg@ver` maps to a bin.
    at = args.index(spec)
    if at and args[at - 1] in ("-p", "--package"):
        return None
    name = npm_name(spec)
    manifest = NPM_PREFIX / "node_modules" / name / "package.json"
    try:
        pkg = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None
    if pkg.get("version") != npm_version(spec):
        return None
    # npx's own choice: a string bin, the sole bin, or the one named for the package.
    bin_path = pkg.get("bin")
    if isinstance(bin_path, dict):
        bins = bin_path
        bin_path = bins.get(name.rsplit("/", 1)[-1])
        if bin_path is None and len(bins) == 1:
            bin_path = next(iter(bins.values()))
    if not isinstance(bin_path, str):
        return None
    return ["node", str((manifest.parent / bin_path).resolve()), *rest]


def installed_uvx(args: List[str]) -> Optional[List[str]]:
    """``<tool exe> <args>`` for a uvx entry whose pinned specs are all installed."""
    spec, with_specs, exe, rest = uvx_specs(args)
    if not spec or not exe or not UV_TOOL_DIR.is_dir():
        return None
    for venv in sorted(p for p in UV_TOOL_DIR.iterdir() if p.is_dir()):
        script = venv / "bin" / exe
        if not script.is_file():
            continue
        site = [str(p) for p in venv.glob("lib/python*/site-packages")]
        dists = list(distributions(path=site))
        if all(_spec_installed(s, dists) for s in [spec, *with_specs]):
            return [str(script), *rest]
    return None


def installed_launch(command: str, args: List[str]) -> Tuple[str, List[str]]:
    """The baked copy's argv for an npx/uvx entry, else ``(command, args)``."""
    # A malformed install must cost only this entry, not the whole catalog load.
    try:
        if command == "npx":
            argv = installed_npx(args)
        elif command == "uvx":
            argv = installed_uvx(args)
        else:
            argv = None
    except (OSError, ValueError, AttributeError, TypeError):
        argv = None
    return (argv[0], argv[1:]) if argv else (command, args)

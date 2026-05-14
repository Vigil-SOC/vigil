"""Vigil SOC Backend API."""

from pathlib import Path


def _read_version() -> str:
    """Read the application version from the top-level VERSION file.

    The VERSION file lives at the repo root in development and at
    /app/VERSION inside the container image (both Dockerfiles `COPY . .`
    from the repo root, so the file ships with the image). Falls back to
    "0.0.0+unknown" if the file is missing or unreadable — e.g. in
    unusual test or partial-install environments. release-please is the
    sole writer of VERSION; see RELEASING.md.
    """
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return version_file.read_text().strip()
    except OSError:
        return "0.0.0+unknown"


__version__ = _read_version()

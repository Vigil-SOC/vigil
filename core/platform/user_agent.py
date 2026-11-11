"""A user agent, read for people."""

from __future__ import annotations

from typing import Optional


def browser_of(user_agent: Optional[str]) -> str:
    """A coarse "Browser on OS" from a user agent, for a list. No dependency."""
    ua = user_agent or ""
    browser = next(
        (
            name
            for token, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("FxiOS", "Firefox"),
                ("CriOS", "Chrome"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if token in ua
        ),
        None,
    )
    os_ = next(
        (
            name
            for token, name in (
                ("iPhone", "iOS"),
                ("iPad", "iOS"),
                ("Android", "Android"),
                ("Windows", "Windows"),
                ("Macintosh", "macOS"),
                ("CrOS", "ChromeOS"),
                ("Linux", "Linux"),
                ("X11", "Linux"),
            )
            if token in ua
        ),
        None,
    )
    if browser and os_:
        return f"{browser} on {os_}"
    return browser or os_ or "Unknown browser"

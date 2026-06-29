"""Shared pytest configuration.

Defaults ``DEV_MODE=true`` for the whole suite before any test module is
imported. Several API tests import FastAPI routers at module load, which
transitively imports ``auth_service`` — and ``auth_service`` raises at import
time if ``DEV_MODE`` is false and ``JWT_SECRET_KEY`` is unset. ``setdefault``
leaves an explicitly-set ``DEV_MODE`` (e.g. a test that exercises production
auth) untouched.
"""

import os

os.environ.setdefault("DEV_MODE", "true")

import pytest  # noqa: E402,F401

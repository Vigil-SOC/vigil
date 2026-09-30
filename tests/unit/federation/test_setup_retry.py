"""Federation's boot setup survives a failure instead of leaving it off."""

from __future__ import annotations

import asyncio
import logging

import pytest

from core.federation import contract, registry
from core.federation import runner as runner_mod
from core.federation.runner import FederationRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def empty_registry(monkeypatch):
    saved = dict(contract._ADAPTER_FACTORIES)
    contract._ADAPTER_FACTORIES.clear()
    monkeypatch.setattr(registry, "_BUILTINS_LOADED", False)
    yield
    contract._ADAPTER_FACTORIES.clear()
    contract._ADAPTER_FACTORIES.update(saved)


def test_one_module_failing_to_import_leaves_the_others_registered(
    empty_registry, monkeypatch, caplog
):
    broken = "core.integrations.aws_security_hub.adapter"

    def import_module(name):
        if name == broken:
            raise ImportError("No module named 'boto3'")
        contract.register_adapter(name, lambda: None)

    monkeypatch.setattr(registry.importlib, "import_module", import_module)

    with caplog.at_level(logging.ERROR, logger=registry.__name__):
        registry._ensure_builtins_loaded()

    registered = set(contract._ADAPTER_FACTORIES)
    assert registered == set(registry._BUILTIN_ADAPTER_MODULES) - {broken}
    assert broken in caplog.text


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(runner_mod, "_SETUP_RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(runner_mod, "_SETUP_RETRY_MAX_SECONDS", 0.01)
    monkeypatch.setattr(runner_mod, "seed_federation_sources", lambda: [])


@pytest.mark.asyncio
async def test_setup_retries_until_the_switch_on_succeeds(fast_retry, monkeypatch):
    calls = []

    def apply_default_on():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("could not connect to server")
        return []

    monkeypatch.setattr(runner_mod, "apply_default_on", apply_default_on)
    runner = FederationRunner(output_queue=None)

    await asyncio.wait_for(runner._setup_until_done(asyncio.Event()), timeout=1)

    assert len(calls) == 3
    assert runner.stats["setup_ok"] is True


@pytest.mark.asyncio
async def test_shutdown_during_the_back_off_stops_the_retries(monkeypatch):
    monkeypatch.setattr(runner_mod, "_SETUP_RETRY_FIRST_SECONDS", 30.0)

    def apply_default_on():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(runner_mod, "apply_default_on", apply_default_on)
    runner = FederationRunner(output_queue=None)
    shutdown = asyncio.Event()

    setup = asyncio.create_task(runner._setup_until_done(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()

    await asyncio.wait_for(setup, timeout=1)
    assert runner.stats["setup_ok"] is False

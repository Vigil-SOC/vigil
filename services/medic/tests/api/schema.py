"""Validate against X2's own schemas (medic-api.openapi.yaml), refs and all."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SPEC_PATH = CONTRACTS / "medic-api.openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())
BASE = SPEC_PATH.resolve().as_uri()


def _registry() -> Registry:
    registry = Registry().with_resource(BASE, Resource(SPEC, specification=DRAFT202012))
    for name in ("decision-record.schema.json", "pack-manifest.schema.json"):
        path = CONTRACTS / name
        doc = json.loads(path.read_text())
        res = Resource(doc, specification=DRAFT202012)
        registry = registry.with_resources(
            [(path.resolve().as_uri(), res), (doc["$id"], res)]
        )
    return registry


REGISTRY = _registry()


def validator(name: str) -> Draft202012Validator:
    """A validator for `#/components/schemas/<name>`."""
    return Draft202012Validator(
        {"$ref": f"{BASE}#/components/schemas/{name}"}, registry=REGISTRY
    )


def errors(name: str, doc) -> list[str]:
    return [e.message for e in validator(name).iter_errors(doc)]

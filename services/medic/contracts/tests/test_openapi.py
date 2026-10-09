"""X2 contract: Medic's API (medic-api.openapi.yaml). K1 T-04, T-06, T-17, T-26, T-27, T-29."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_openapi
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).parent.parent
SPEC_PATH = ROOT / "medic-api.openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())
BASE = SPEC_PATH.resolve().as_uri()
CHAIN_01 = ROOT / "fixtures" / "decision-records" / "valid" / "chain-01-pilot-day.jsonl"
EXTERNAL = ["decision-record.schema.json", "pack-manifest.schema.json"]

METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
ALLOWED_TAGS = {
    "enum",
    "number",
    "id",
    "timestamp",
    "hash",
    "redacted_excerpt",
    "pack_text",
    "admin_input",
}
# One status per error code (the Error schema's description is the human-readable copy).
ERROR_STATUS = {
    "unauthorized": 401,
    "not_found": 404,
    "method_not_allowed": 405,
    "invalid_parameter": 400,
    "invalid_cursor": 400,
    "invalid_body": 422,
    "missing_admin_identity": 422,
    "not_rateable": 409,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "busy": 503,
    "store_not_recording": 503,
    "internal": 500,
}


def _registry() -> Registry:
    registry = Registry().with_resource(BASE, Resource(SPEC, specification=DRAFT202012))
    for name in EXTERNAL:
        path = ROOT / name
        doc = json.loads(path.read_text())
        res = Resource(doc, specification=DRAFT202012)
        registry = registry.with_resources(
            [(path.resolve().as_uri(), res), (doc["$id"], res)]
        )
    return registry


REGISTRY = _registry()


def _validator(pointer: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"{BASE}#{pointer}"}, registry=REGISTRY)


def _esc(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def operations():
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                yield path, method, op


def _deref_local(node: dict) -> dict:
    while isinstance(node, dict) and "$ref" in node and node["$ref"].startswith("#/"):
        target = SPEC
        for part in node["$ref"][2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = target
    return node


def media_examples():
    """(id, schema pointer, example value) for every example attached to a media type."""
    for path, method, op in operations():
        base = f"/paths/{_esc(path)}/{method}"
        bodies = []
        if "requestBody" in op:
            bodies.append((f"{base}/requestBody", op["requestBody"]))
        for code, resp in op["responses"].items():
            bodies.append((f"{base}/responses/{code}", resp))
        for ptr, holder in bodies:
            if "$ref" in holder:
                ptr = holder["$ref"][1:]
                holder = _deref_local(holder)
            for mtype, media in holder.get("content", {}).items():
                examples = media.get("examples", {})
                assert examples, f"{ptr} {mtype} has no example"
                for name, ex in examples.items():
                    ex = _deref_local(ex)
                    schema_ptr = f"{ptr}/content/{_esc(mtype)}/schema"
                    yield f"{op['operationId']}:{name}", schema_ptr, ex["value"], mtype


EXAMPLES = list(media_examples())


# --- The spec is valid OpenAPI 3.1 and every $ref resolves ------------------------------


def test_spec_is_valid_openapi_3_1() -> None:
    assert SPEC["openapi"].startswith("3.1.")
    validate_openapi(SPEC, base_uri=BASE)


def _refs(node, ptr=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                yield ptr, v
            elif k != "value":  # example payloads are data, not schema
                yield from _refs(v, f"{ptr}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _refs(v, f"{ptr}/{i}")


def test_every_ref_resolves() -> None:
    resolver = REGISTRY.resolver(base_uri=BASE)
    refs = list(_refs(SPEC))
    assert len(refs) > 100
    bad = []
    for where, ref in refs:
        try:
            resolver.lookup(ref)
        except Exception as exc:  # noqa: BLE001 - report every failure at once
            bad.append((where, ref, type(exc).__name__))
    assert bad == []


def test_external_refs_point_only_at_existing_contracts() -> None:
    files = {ref.split("#")[0] for _, ref in _refs(SPEC) if not ref.startswith("#")}
    assert files == {f"./{name}" for name in EXTERNAL}


def test_responses_reuse_the_decision_record_schema_instead_of_redefining_it() -> None:
    record = "./decision-record.schema.json"
    schemas = SPEC["components"]["schemas"]
    assert schemas["DecisionPage"]["properties"]["items"]["items"] == {"$ref": record}
    assert schemas["IncidentDetail"]["properties"]["opened"]["$ref"] == record
    assert schemas["FeedbackAccepted"]["properties"]["record"] == {"$ref": record}
    get_one = SPEC["paths"]["/v1/decisions/{seq}"]["get"]["responses"]["200"]
    assert get_one["content"]["application/json"]["schema"] == {"$ref": record}
    # Nothing in the API re-declares a record envelope field.
    for name, schema in schemas.items():
        props = set(schema.get("properties", {}))
        assert not {"prev", "hash", "body"} <= props, name


# --- Examples validate ------------------------------------------------------------------


def test_every_operation_and_error_response_has_examples() -> None:
    ops = {e[0].split(":")[0] for e in EXAMPLES}
    assert ops == {op["operationId"] for _, _, op in operations()}
    assert len(EXAMPLES) >= 30


@pytest.mark.parametrize("name,ptr,value,mtype", EXAMPLES, ids=[e[0] for e in EXAMPLES])
def test_example_validates_against_its_schema(name, ptr, value, mtype) -> None:
    errors = sorted(_validator(ptr).iter_errors(value), key=str)
    assert errors == [], [e.message for e in errors[:5]]


def test_example_records_are_the_real_chain_01_records() -> None:
    """Examples are copied from a valid fixture chain, so their hashes are real."""
    fixture = {r["seq"]: r for r in map(json.loads, CHAIN_01.read_text().splitlines())}
    examples = SPEC["components"]["examples"]
    for name in ["RecordOpened", "RecordFeedback", "RecordResolving", "RecordResolved"]:
        rec = examples[name]["value"]
        assert rec == fixture[rec["seq"]], name


@pytest.mark.parametrize(
    "body",
    [
        {"value": "agree", "admin": "user:1"},  # forged identity (T-17)
        {"value": "agree", "user": "user:1"},
        {"value": "agree", "source": "replay"},  # v1 is live only
        {"value": "agree", "suggested_lane": 3},  # suggested_lane only with wrong_lane
        {"value": "maybe"},
        {"value": "wrong_lane", "suggested_lane": 4},
        {"value": "agree", "comment": "x" * 501},
        {"value": "agree", "comment": ""},
        {"comment": "no value"},
    ],
)
def test_feedback_request_rejects(body) -> None:
    ptr = "/components/schemas/FeedbackRequest"
    assert not _validator(ptr).is_valid(body)


def test_error_examples_use_one_status_per_code() -> None:
    enum = SPEC["components"]["schemas"]["Error"]["properties"]["code"]["enum"]
    assert set(enum) == set(ERROR_STATUS)
    errors = [(n, v) for n, _, v, m in EXAMPLES if m == "application/problem+json"]
    assert len(errors) >= 10
    for name, err in errors:
        assert err["status"] == ERROR_STATUS[err["code"]], name
        assert err["type"] == f"urn:medic:error:{err['code']}", name
        assert ("reason" in err) == (err["code"] == "not_rateable"), name


# --- Surface: one caller, one write, permissions named for H3 ----------------------------


def test_every_route_is_versioned_v1() -> None:
    assert all(path.startswith("/v1/") for path in SPEC["paths"])


def test_exactly_one_write_and_it_is_admin_feedback() -> None:
    writes = [(p, m, op) for p, m, op in operations() if m != "get"]
    assert [(p, m) for p, m, _ in writes] == [
        ("/v1/incidents/{incident_id}/feedback", "post")
    ]
    assert writes[0][2]["x-medic-permission"] == "admin"


def test_every_operation_names_the_permission_h3_must_check() -> None:
    perms = {
        op["operationId"]: op.get("x-medic-permission") for _, _, op in operations()
    }
    assert set(perms.values()) <= {"read", "admin"}, perms
    admin = {k for k, v in perms.items() if v == "admin"}
    assert admin == {"postFeedback", "getEvidenceExport"}


def test_no_route_skips_the_shared_secret() -> None:
    assert SPEC["security"] == [{"medicKey": []}]
    scheme = SPEC["components"]["securitySchemes"]["medicKey"]
    assert scheme == {**scheme, "type": "apiKey", "in": "header", "name": "X-Medic-Key"}
    for _, _, op in operations():
        assert "security" not in op, op["operationId"]
        assert "401" in op["responses"], op["operationId"]


def test_feedback_identity_comes_from_a_header_not_the_body() -> None:
    op = SPEC["paths"]["/v1/incidents/{incident_id}/feedback"]["post"]
    headers = [p for p in op["parameters"] if p.get("in") == "header"]
    assert [(h["name"], h["required"]) for h in headers] == [("X-Medic-Admin", True)]
    body = SPEC["components"]["schemas"]["FeedbackRequest"]
    assert body["additionalProperties"] is False
    assert set(body["properties"]) == {"value", "suggested_lane", "comment"}


def test_lists_are_paginated_and_bounded() -> None:
    paged = {"listIncidents": 200, "listDecisions": 100}
    for _, _, op in operations():
        params = {
            p.get("name") or p["$ref"].split("/")[-1]: p
            for p in op.get("parameters", [])
        }
        if op["operationId"] in paged:
            assert "Cursor" in params, op["operationId"]
            assert params["limit"]["schema"]["maximum"] == paged[op["operationId"]]
    schemas = SPEC["components"]["schemas"]

    def arrays(node, path):
        node = _deref_local(node)
        if node.get("type") == "array":
            yield path, node
        for k, sub in node.get("properties", {}).items():
            yield from arrays(sub, f"{path}.{k}")
        for b in node.get("oneOf", []):
            yield from arrays(b, path)

    found = [a for name, s in schemas.items() for a in arrays(s, name)]
    assert found
    for path, node in found:
        assert "maxItems" in node, path


def test_only_json_media_types_and_errors_are_problem_json() -> None:
    for _, _, op in operations():
        for code, resp in op["responses"].items():
            resp = _deref_local(resp)
            types = set(resp.get("content", {}))
            if code.startswith("2"):
                assert types == {"application/json"}, (op["operationId"], code)
            else:
                assert types == {"application/problem+json"}, (op["operationId"], code)
                schema = resp["content"]["application/problem+json"]["schema"]
                assert schema == {"$ref": "#/components/schemas/Error"}


# --- K1 T-04 / T-06: every field typed, free text only from packs or K2 -----------------


def _leaves(node, path):
    if "$ref" in node:
        if node["$ref"].startswith("#/"):
            yield from _leaves(_deref_local(node), path)
        else:
            yield path, "external"  # typed by its own contract's tests
        return
    if "x-medic-type" in node:
        yield path, node["x-medic-type"]
        return
    walked = False
    for key, sub in node.get("properties", {}).items():
        walked = True
        yield from _leaves(sub, f"{path}.{key}")
    if "items" in node:
        walked = True
        yield from _leaves(node["items"], f"{path}[]")
    for i, branch in enumerate(node.get("oneOf", []) + node.get("anyOf", [])):
        walked = True
        yield from _leaves(branch, f"{path}|{i}")
    if not walked and node.get("type") != "object":
        yield path, None


def test_every_leaf_field_carries_an_allowed_type_tag() -> None:
    schemas = SPEC["components"]["schemas"]
    leaves = [leaf for name, s in schemas.items() for leaf in _leaves(s, name)]
    untagged = [p for p, t in leaves if t is None]
    unknown = [(p, t) for p, t in leaves if t not in ALLOWED_TAGS | {"external", None}]
    assert untagged == [], untagged
    assert unknown == [], unknown
    assert len(leaves) > 100


def test_admin_input_only_in_the_feedback_request() -> None:
    schemas = SPEC["components"]["schemas"]
    where = [
        p for n, s in schemas.items() for p, t in _leaves(s, n) if t == "admin_input"
    ]
    assert where == ["FeedbackRequest.comment"]


def test_free_text_in_responses_is_only_pack_text_or_redacted() -> None:
    schemas = SPEC["components"]["schemas"]

    def strings(node, path):
        node = _deref_local(node)
        if node.get("type") == "string" and not {"pattern", "enum", "const"} & set(
            node
        ):
            yield path, node.get("x-medic-type")
        for k, sub in node.get("properties", {}).items():
            yield from strings(sub, f"{path}.{k}")
        if "items" in node:
            yield from strings(node["items"], f"{path}[]")
        for b in node.get("oneOf", []) + node.get("anyOf", []):
            yield from strings(b, path)

    found = [
        s
        for n, sch in schemas.items()
        if n != "FeedbackRequest"
        for s in strings(sch, n)
    ]
    assert ("IncidentSummary.title", "pack_text") in found
    assert {tag for _, tag in found} <= {"pack_text", "redacted_excerpt"}, found

"""An ARRAY column and the schema that serializes it agree on the element type.

Pydantic v2 does not coerce, so a column of `ARRAY(Integer)` described as a
list of `str` raises `ValidationError` on every row that is not empty, and
`services/api/errors.py` renders that as "Internal server error". It is not an
edge case: the four seeded SLA policies all carry `[75, 90, 100]`.

Named columns would go stale. This sweeps every registered model instead, so a
new ARRAY column arrives with its schema already checked.

The sweep refuses an element type it has no rule for rather than skipping it. A
skip here reads as a pass: the next `ARRAY(Float)` or `ARRAY(UUID)` column would
arrive green with nothing checked, which is the hole this file exists to close.
"""

from __future__ import annotations

import typing

import pytest
from sqlalchemy import ARRAY, Integer, String, Text
from sqlalchemy.inspection import inspect as sa_inspect

from tests.unit.storage.test_orm_schema_parity import SCHEMA_REGISTRY

pytestmark = pytest.mark.unit

# The SQLAlchemy element types in use, and what Pydantic must call them.
_ELEMENT_TYPES = {Integer: int, String: str, Text: str}


def _python_element_type(item_type) -> type | None:
    for sa_type, py_type in _ELEMENT_TYPES.items():
        if isinstance(item_type, sa_type):
            return py_type
    return None


def _declared_element_type(annotation):
    """The `X` in the field's `list[X]`, through Optional and Annotated."""
    seen = set()
    stack = [annotation]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        origin = typing.get_origin(current)
        if origin in (list, typing.List):
            args = typing.get_args(current)
            return args[0] if args else None
        stack.extend(a for a in typing.get_args(current) if a is not type(None))
    return None


def _schema_of(spec):
    """The registry holds bound `Schema.dump` methods; the class is on `__self__`.

    Only the first is taken, so a model serialized by several schemas has the
    rest unchecked.
    """
    for case in spec["cases"].values():
        owner = getattr(case, "__self__", None)
        if owner is not None:
            return owner
    return None


def _array_columns():
    for model_name, spec in sorted(SCHEMA_REGISTRY.items()):
        model, schema = spec["model"], _schema_of(spec)
        for column in sa_inspect(model).mapper.columns:
            if isinstance(column.type, ARRAY):
                yield model_name, schema, column


@pytest.mark.parametrize(
    "model_name,schema,column",
    list(_array_columns()),
    ids=lambda v: getattr(v, "key", None) or getattr(v, "__name__", str(v)),
)
def test_the_schema_declares_the_element_type_the_column_stores(
    model_name, schema, column
):
    assert schema is not None, (
        f"{model_name} is in the registry with no schema this can recover, so "
        f"its ARRAY columns are swept and nothing is asserted about them."
    )

    expected = _python_element_type(column.type.item_type)
    assert expected is not None, (
        f"{model_name}.{column.key} is "
        f"ARRAY({type(column.type.item_type).__name__}), which this sweep has "
        f"no rule for -- so it would be carried past unchecked. Add it to "
        f"_ELEMENT_TYPES with the Python type Pydantic must be told, and teach "
        f"the ORM sample generator to build a value for it."
    )

    field = schema.model_fields.get(column.key)
    if field is None:
        pytest.skip(f"{model_name}.{column.key} is not serialized")

    declared = _declared_element_type(field.annotation)
    assert declared is expected, (
        f"{model_name}.{column.key} is ARRAY({type(column.type.item_type).__name__}) "
        f"but {schema.__name__}.{column.key} declares list[{declared}]. "
        "Pydantic v2 does not coerce, so every non-empty row raises."
    )


def test_a_seeded_sla_policy_serializes():
    """The shape the baseline actually ships: `[75, 90, 100]`, not an edge case."""
    from core.storage.models import SLAPolicy
    from core.storage.schemas.case_entities import SLAPolicySchema

    policy = SLAPolicy(
        policy_id="sla-1",
        name="High",
        priority_level="high",
        notification_thresholds=[75, 90, 100],
    )

    assert SLAPolicySchema.dump(policy)["notification_thresholds"] == [75, 90, 100]


def test_a_comment_with_attachments_serializes():
    from core.storage.models import CaseComment
    from core.storage.schemas.case_entities import CaseCommentSchema

    comment = CaseComment(comment_id=1, case_id="CASE-1", attachment_ids=[1, 2])

    assert CaseCommentSchema.dump(comment)["attachment_ids"] == [1, 2]

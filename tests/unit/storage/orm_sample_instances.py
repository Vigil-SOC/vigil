"""Deterministic in-memory ORM instances for serialization parity tests.

Builds every model in ``database.models`` twice — once with every column
populated, once bare — without touching a database. Transient SQLAlchemy
objects are enough to exercise serialization, which matters because these
models use Postgres ARRAY/JSONB/pgvector and cannot be created on SQLite.

The bare ("empty") variant is the interesting one: it pins the ``or []`` /
``or {}`` / ``float(x or 0)`` coercions that turn NULL columns into empty
containers and zeroes in the JSON contract.
"""

import inspect as pyi
import re
import zlib
from datetime import datetime, timezone

from sqlalchemy import inspect as sa_inspect

import core.storage.models as models

# Fixed so snapshots are byte-stable across runs.
FIXED_DT = datetime(2024, 3, 14, 15, 9, 26, 535000, tzinfo=timezone.utc)


def iter_serializable_models():
    """Yield (name, class) for every mapped ORM model."""
    for name in sorted(dir(models)):
        obj = getattr(models, name)
        if pyi.isclass(obj) and hasattr(obj, "__tablename__"):
            yield name, obj


def _schema_for(model):
    """The ``{Model}Schema`` that owns this model's contract, or None."""
    import core.storage.schemas as schemas

    return getattr(schemas, f"{model.__name__}Schema", None)


def _json_default_is_list(model, column_key):
    """True when a JSON column serializes as a list, False for a map.

    The schema field type is the authority — ``JsonList``/``StrList`` resolve
    to ``list`` and ``JsonDict`` to ``dict``. A pass-through ``Optional[Any]``
    field constrains nothing, so the model's ``Mapped[...]`` annotation breaks
    the tie. Returns None when neither settles it.
    """
    schema = _schema_for(model)
    if schema is not None and column_key in schema.model_fields:
        field_type = str(schema.model_fields[column_key].annotation)
        if re.search(r"\blist\b", field_type):
            return True
        if re.search(r"\bdict\b", field_type):
            return False

    for klass in model.__mro__:
        annotation = getattr(klass, "__annotations__", {}).get(column_key)
        if annotation is None:
            continue
        text = annotation if isinstance(annotation, str) else str(annotation)
        if re.search(r"\b(List|list)\[", text):
            return True
        if re.search(r"\b(Dict|dict)\b", text):
            return False
    return None


def _stable_int(key, modulo=1000):
    return zlib.crc32(key.encode()) % modulo


def _value_for(model, model_name, column):
    """Deterministic sample value for one column, keyed off its type."""
    key = f"{model_name}.{column.key}"
    type_name = type(column.type).__name__

    if type_name in ("String", "Text"):
        return f"{column.key}-value"
    if type_name == "Integer":
        return _stable_int(key)
    if type_name in ("Float", "Numeric"):
        return round(_stable_int(key) / 100, 2)
    if type_name == "Boolean":
        return True
    if type_name == "DateTime":
        return FIXED_DT
    if type_name == "VECTOR":
        return [0.1, 0.2, 0.3]
    if type_name == "ARRAY":
        return [f"{column.key}-a", f"{column.key}-b"]
    if type_name == "JSONB":
        if _json_default_is_list(model, column.key):
            return [{"item": f"{column.key}-0"}]
        return {"sample": f"{column.key}-value"}
    raise AssertionError(f"no sample value rule for {key} ({type_name})")


def build_populated(model_name, model):
    """Instance with every mapped column set to a deterministic value."""
    kwargs = {
        col.key: _value_for(model, model_name, col)
        for col in sa_inspect(model).mapper.columns
    }
    return model(**kwargs)


def build_empty(model):
    """Bare instance — every column NULL, every relationship empty."""
    return model()


def build_related(model_name, model):
    """Populate the relationships that serialization actually reads.

    Only ``Case.findings`` and ``Conversation.messages`` are traversed by the
    serializers, so only those get children; everything else round-trips as
    the populated instance.
    """
    instance = build_populated(model_name, model)
    if model_name == "Case":
        instance.findings = [build_populated("Finding", models.Finding)]
    elif model_name == "Conversation":
        instance.messages = [build_populated("ChatMessage", models.ChatMessage)]
    return instance

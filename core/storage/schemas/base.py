"""Base class and field types for the ORM-mode serialization schemas.

These schemas own the JSON contract for the models in ``database.models``.
They live beside the models rather than under ``backend/`` because the
daemon and the service layer serialize the same objects and should not have
to import the web layer to do it.

The field aliases below exist to reproduce the coercions the contract has
always applied: NULL columns surface as empty containers, Numeric columns as
plain floats, and datetimes as ``datetime.isoformat()`` output.
"""

from datetime import datetime
from typing import Annotated, Any, Iterable, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer


def _none_to_list(value: Any) -> Any:
    return [] if value is None else value


def _none_to_dict(value: Any) -> Any:
    return {} if value is None else value


def _none_to_zero(value: Any) -> Any:
    return 0.0 if value is None else value


def _as_plain_list(value: Any) -> Any:
    """pgvector hands back a numpy array; JSON needs a plain list."""
    if value is not None and hasattr(value, "tolist"):
        return value.tolist()
    return value


# ``datetime`` serialized to JSON by Pydantic renders UTC as "...Z", while the
# established contract is isoformat()'s "...+00:00". Serialize explicitly so
# the two never diverge.
IsoDateTime = Annotated[
    datetime,
    PlainSerializer(lambda v: v.isoformat(), return_type=str, when_used="json"),
]

OptDateTime = Optional[IsoDateTime]

# Containers that must never serialize as null.
JsonList = Annotated[list, BeforeValidator(_none_to_list)]
JsonDict = Annotated[dict, BeforeValidator(_none_to_dict)]
StrList = Annotated[list[str], BeforeValidator(_none_to_list)]

# Numeric/Float columns whose contract floors NULL at zero.
ZeroFloat = Annotated[float, BeforeValidator(_none_to_zero)]

# Nullable Boolean columns the contract narrows to a real bool.
CoercedBool = Annotated[bool, BeforeValidator(bool)]

Embedding = Annotated[Optional[list[float]], BeforeValidator(_as_plain_list)]


class ORMSchema(BaseModel):
    """Read model over a SQLAlchemy instance.

    ``dump`` is the serialization entry point — it always emits JSON-mode
    output with aliases applied, which is what API responses expect.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @classmethod
    def dump(cls, obj: Any, **kwargs: Any) -> dict:
        """Serialize one ORM instance to a JSON-safe dict."""
        return cls.model_validate(obj).model_dump(mode="json", by_alias=True, **kwargs)

    @classmethod
    def dump_many(cls, objs: Iterable[Any], **kwargs: Any) -> list[dict]:
        """Serialize an iterable of ORM instances."""
        return [cls.dump(obj, **kwargs) for obj in objs]

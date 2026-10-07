"""Base class and field types for the ORM-mode serialization schemas.

These schemas own the JSON contract for the models in ``database.models``.
They live beside the models rather than under ``services/api/`` because the
daemon and the service layer serialize the same objects and should not have
to import the web layer to do it.

The field aliases below exist to reproduce the coercions the contract has
always applied: NULL columns surface as empty containers, Numeric columns as
plain floats, and datetimes as ``datetime.isoformat()`` output.
"""

from datetime import datetime, timezone
from typing import Annotated, Any, Iterable, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer


def _none_to_list(value: Any) -> Any:
    return [] if value is None else value


def _none_to_dict(value: Any) -> Any:
    return {} if value is None else value


def _none_to_zero(value: Any) -> Any:
    return 0.0 if value is None else value


# The columns behind these are `timestamp without time zone` holding UTC, so a
# naive value's offset is known and merely unwritten. Stamped before serializing
# because a reader parsing one without it lands in its own zone instead --
# JavaScript's Date does exactly that, which put every run in the console's
# history off by the viewer's offset. An aware value already says so.
def _iso_utc(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


# ``datetime`` serialized to JSON by Pydantic renders UTC as "...Z", while the
# established contract is isoformat()'s "...+00:00". Serialize explicitly so
# the two never diverge.
IsoDateTime = Annotated[
    datetime,
    PlainSerializer(_iso_utc, return_type=str, when_used="json"),
]

OptDateTime = Optional[IsoDateTime]

# Containers that must never serialize as null.
JsonList = Annotated[list, BeforeValidator(_none_to_list)]
JsonDict = Annotated[dict, BeforeValidator(_none_to_dict)]
StrList = Annotated[list[str], BeforeValidator(_none_to_list)]
# For ARRAY(Integer) columns. Pydantic v2 does not coerce, so a list of ints
# described as StrList raises on every non-empty row.
IntList = Annotated[list[int], BeforeValidator(_none_to_list)]

# Numeric/Float columns whose contract floors NULL at zero.
ZeroFloat = Annotated[float, BeforeValidator(_none_to_zero)]

# Nullable Boolean columns the contract narrows to a real bool.
CoercedBool = Annotated[bool, BeforeValidator(bool)]


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

"""Bind a whole list as one Postgres array parameter."""

from typing import Iterable

from sqlalchemy import String, bindparam
from sqlalchemy.dialects.postgresql import ARRAY


def text_array(values: Iterable[str]):
    # One bind parameter however many values: an expanding IN would add one each.
    return bindparam(None, list(values), type_=ARRAY(String))

"""When a closed case was closed. The one formula.

Case metrics and the Production Start Report both read it. A second
expression would drift the first time one of them is edited.
"""

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from core.storage.models.case import Case
from core.storage.models.case_entities import CaseClosureInfo


def closed_at() -> ColumnElement:
    """``case_closure_info.closed_at``, else ``cases.updated_at``.

    ``close_case`` stamps ``closed_at``, and it does not move when the case
    is edited afterwards. A case closed before closure rows existed falls
    back to ``cases.updated_at``.
    """
    return func.coalesce(CaseClosureInfo.closed_at, Case.updated_at)

"""Every cases route must declare a response_model so OpenAPI is a real contract."""

import pytest
from fastapi.routing import APIRoute

from core.storage.schemas import CaseSchema, CaseWithFindingsSchema
from core.api.v1 import cases_router as v1_cases
from services.api.routers import cases

pytestmark = pytest.mark.unit


def test_every_cases_route_declares_response_model():
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in [*cases.router.routes, *v1_cases.router.routes]
        if isinstance(route, APIRoute) and route.response_model is None
    ]
    assert not missing, "cases routes without response_model:\n  " + "\n  ".join(
        missing
    )


def test_get_case_documents_ids_not_inlined_findings():
    """GET /cases/{id} dumps CaseSchema (finding_ids). Inlined findings would
    be a behaviour change — DatabaseDataService.get_case loads findings then
    still dumps CaseSchema.
    """
    get_case = [
        route
        for route in [*cases.router.routes, *v1_cases.router.routes]
        if isinstance(route, APIRoute)
        and route.path == "/{case_id}"
        and "GET" in route.methods
    ]
    assert len(get_case) == 1
    assert get_case[0].response_model is CaseSchema
    assert get_case[0].response_model is not CaseWithFindingsSchema

"""The signed-in person's notices and live conditions, behind the console's bell."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.platform import notices
from core.routing import Auth, RouterMeta
from core.storage.models import User
from services.api.middleware.auth import get_current_active_user

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/notices", tags=["notices"], auth=Auth.REQUIRED)


class NoticesReadRequest(BaseModel):
    """Which notices to mark read: ``ids``, or all of the person's own."""

    ids: Optional[List[int]] = Field(default=None, max_length=1000)
    all: bool = False


@router.get("")
def get_notices(user: User = Depends(get_current_active_user)):
    """This person's notices (unread first, a few read ones after) and the live conditions."""
    return notices.listing(user)


@router.post("/read")
def read_notices(
    body: NoticesReadRequest, user: User = Depends(get_current_active_user)
):
    """Marks this person's own notices read; returns the listing."""
    if body.all == (body.ids is not None):
        raise HTTPException(status_code=422, detail="Send ids or all, not both.")
    return notices.mark_read(user, None if body.all else body.ids)

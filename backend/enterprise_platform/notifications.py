"""通知中心共享 router factory。"""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from enterprise_platform.ports import NotificationPort
from enterprise_platform.schemas import NotificationPage


def create_notification_router(
    provider: NotificationPort,
    *,
    current_user_id: Callable[[], str],
) -> APIRouter:
    router = APIRouter(prefix="/notifications", tags=["notifications"])

    @router.get("", response_model=NotificationPage)
    def list_notifications(
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        user_id: str = Depends(current_user_id),
    ) -> NotificationPage:
        return provider.list_notifications(user_id, cursor=cursor, limit=limit)

    @router.post("/{notification_id}/read")
    def mark_read(notification_id: str, user_id: str = Depends(current_user_id)) -> dict[str, bool]:
        if not provider.mark_read(user_id, notification_id, read_at=datetime.now(UTC)):
            raise HTTPException(404, "通知不存在")
        return {"ok": True}

    @router.post("/read-all")
    def mark_all_read(user_id: str = Depends(current_user_id)) -> dict[str, int]:
        return {"updated": provider.mark_all_read(user_id, read_at=datetime.now(UTC))}

    return router

"""EasyAuth 通知调用的失败类型。

F1 的 ``NotifyClient`` 必须抛出本模块的同类异常,F3 的 outbox 驱动按类型分类,
两边不得各自定义一套平行错误。
"""

from __future__ import annotations


class NotifyError(Exception):
    """通知通道失败的基类。"""


class NotifyDedupConflictError(NotifyError):
    """同一 ``dedup_key`` 命中但载荷不一致(HTTP 409)。永久失败。"""


class NotifyRejectedError(NotifyError):
    """永久拒绝:HTTP 401 / 403 / 422,以及查询时消息不存在的 404,不得重试。

    F1 无独立 ``NotifyNotFoundError``;GET 404 同样抛本类且 ``status=404``。
    """

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotifyThrottledError(NotifyError):
    """HTTP 429 限流;``retry_after`` 为秒,缺省则由调用方走退避。"""

    def __init__(self, retry_after: float | int | None = None) -> None:
        super().__init__("通知接口限流")
        if retry_after is not None:
            retry_after = float(retry_after)
            if retry_after < 0:
                raise ValueError("retry_after 不能为负")
        self.retry_after = retry_after


class NotifyUnavailableError(NotifyError):
    """网络故障或 HTTP 5xx / 503,稍后可重试。"""

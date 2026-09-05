"""上游健康检查路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from enterprise_platform.assembly.contracts import UPSTREAM_MANAGE, UPSTREAM_VIEW
from enterprise_platform.assembly.dependencies import AssemblyDependencies
from enterprise_platform.schemas import CurrentUser, UpstreamHealthItem


def register_ops_routes(router: APIRouter, ctx: AssemblyDependencies) -> None:
    _register_upstream_health(router, ctx)
    _register_check_upstreams(router, ctx)


def _register_upstream_health(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.get("/ops/upstream-health", response_model=list[UpstreamHealthItem], tags=["ops"])
    def upstream_health(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(UPSTREAM_VIEW)),
    ) -> list[UpstreamHealthItem]:
        return ctx.port_call(ctx.ports.upstream_health.latest)


def _register_check_upstreams(router: APIRouter, ctx: AssemblyDependencies) -> None:
    @router.post("/ops/upstream-health/checks", response_model=list[UpstreamHealthItem], tags=["ops"])
    def check_upstreams(
        user: CurrentUser = Depends(ctx.current_user),
        _permission: Any = Depends(ctx.permission_for(UPSTREAM_MANAGE)),
    ) -> list[UpstreamHealthItem]:
        result = ctx.port_call(lambda: ctx.ports.upstream_health.run_checks(actor_id=user.id))
        ctx.hooks.after_event(user.id, "ops.upstream_health.check", None, {"count": len(result)})
        return result

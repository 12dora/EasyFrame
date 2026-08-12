"""blank host EasyAuth descriptor / manifest 与同步密钥。"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from blank_app.adapters import _get_setting, record_platform_audit
from blank_app.database import SessionLocal
from blank_app.models import DescriptorKey, PermissionCatalog
from enterprise_platform.schemas import (
    DescriptorKeyCreateRequest,
    DescriptorKeyCreateResponse,
    DescriptorKeyResponse,
    DescriptorKeyUpdateRequest,
)


def _facade():
    import blank_app.authz_api as authz_api

    return authz_api


descriptor_router = APIRouter()


def _current_manifest() -> dict:
    """生成与 EasyAuth app SDK 0.3 descriptor 兼容的当前 manifest。"""

    configured_app_key = str(_get_setting("easyauth").get("app_key") or _facade().FRAMEWORK_MANIFEST.app_key)
    with SessionLocal() as db:
        catalog = {row.code: row for row in db.query(PermissionCatalog).all()}
    scopes = sorted(
        {
            scope.value
            for permission in _facade().FRAMEWORK_MANIFEST.permissions
            for scope in permission.supported_scopes
        }
    )
    domains = sorted({permission.domain for permission in _facade().FRAMEWORK_MANIFEST.permissions})
    return {
        "schema_version": _facade().FRAMEWORK_MANIFEST.schema_version,
        "app": {
            "app_key": configured_app_key,
            "name": "Enterprise Blank",
            "description": "Reusable enterprise application framework",
            "is_active": True,
        },
        "scopes": [{"key": scope, "name": scope, "name_en": scope} for scope in scopes],
        "permission_groups": [
            {"key": domain, "name": domain, "name_en": domain, "parent_key": None} for domain in domains
        ],
        "permissions": [
            {
                "key": permission.code,
                "name": catalog[permission.code].name_zh if permission.code in catalog else permission.code,
                "name_en": catalog[permission.code].name_en if permission.code in catalog else permission.code,
                "group_key": permission.domain,
                "supported_scopes": [scope.value for scope in permission.supported_scopes],
                "risk_level": permission.risk_level,
                "is_active": permission.active,
            }
            for permission in _facade().FRAMEWORK_MANIFEST.permissions
        ],
        "authorization_groups": [],
        "approval_rules": [],
        "capabilities": ["directory", "notify"],
    }


def _validate_descriptor_token(token: str | None) -> bool:
    with SessionLocal() as db:
        active = db.query(DescriptorKey).filter(DescriptorKey.active.is_(True)).all()
        if not active:
            return os.getenv("BLANK_RUNTIME_ENV", "production").strip().lower() != "production"
        if not token:
            return False
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        matched = next((key for key in active if secrets.compare_digest(key.token_hash, token_hash)), None)
        if matched is None:
            return False
        matched.last_used_at = datetime.now(UTC)
        db.commit()
        return True


@descriptor_router.get("/.well-known/easyauth-app.json", include_in_schema=False)
def easyauth_descriptor(request: Request) -> JSONResponse:
    authorization = request.headers.get("authorization")
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :].strip() or None
    if not _validate_descriptor_token(token):
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "descriptor_unauthorized", "message": "描述符访问未授权。"}},
        )
    manifest_payload = _current_manifest()
    return JSONResponse(
        {
            "descriptor_version": 1,
            "app": {
                "app_key": manifest_payload["app"]["app_key"],
                "name": manifest_payload["app"]["name"],
                "description": manifest_payload["app"]["description"],
            },
            "manifest": manifest_payload,
            "sdk": {"name": "easyauth-app-sdk-python", "version": "0.3.0"},
        }
    )


def _list_descriptor_keys() -> list[DescriptorKey]:
    with SessionLocal() as db:
        return db.query(DescriptorKey).order_by(DescriptorKey.created_at.desc()).all()


def _create_descriptor_key(payload: DescriptorKeyCreateRequest, *, actor_id: str) -> DescriptorKeyCreateResponse:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "密钥名称不能为空")
    token = f"epd_{secrets.token_urlsafe(32)}"
    with SessionLocal() as db:
        row = DescriptorKey(
            name=name,
            token_prefix=token[:10],
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            active=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        response = DescriptorKeyResponse.model_validate(row)
    record_platform_audit(actor_id, "authz.descriptor_key.create", None, {"id": response.id, "name": name})
    return DescriptorKeyCreateResponse(key=response, token=token)


def _update_descriptor_key(key_id: uuid.UUID, payload: DescriptorKeyUpdateRequest, *, actor_id: str) -> DescriptorKey:
    with SessionLocal() as db:
        row = db.get(DescriptorKey, key_id)
        if row is None:
            raise HTTPException(404, "描述符同步密钥不存在")
        before = {"active": row.active}
        row.active = payload.active
        db.commit()
        db.refresh(row)
        db.expunge(row)
    record_platform_audit(actor_id, "authz.descriptor_key.update", before, {"active": payload.active})
    return row


def _delete_descriptor_key(key_id: uuid.UUID, *, actor_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(DescriptorKey, key_id)
        if row is None:
            raise HTTPException(404, "描述符同步密钥不存在")
        before = {"name": row.name, "tokenPrefix": row.token_prefix, "active": row.active}
        db.delete(row)
        db.commit()
    record_platform_audit(actor_id, "authz.descriptor_key.delete", before, None)


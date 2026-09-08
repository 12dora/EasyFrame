"""blank host EasyAuth descriptor / manifest 与同步密钥。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from blank_app.models import DescriptorKey
from enterprise_platform.schemas import (
    DescriptorKeyCreateRequest,
    DescriptorKeyCreateResponse,
    DescriptorKeyUpdateRequest,
)


def _facade():
    from blank_app import authz_api

    return authz_api


descriptor_router = APIRouter()


def _manifest_app(app_key: str) -> dict:
    return {
        "app_key": app_key,
        "name": "Enterprise Blank",
        "description": "Reusable enterprise application framework",
        "is_active": True,
    }


def _current_manifest() -> dict:
    """生成与 EasyAuth app SDK 0.3 descriptor 兼容的当前 manifest。"""

    configured_app_key = str(_facade()._get_setting("easyauth").get("app_key") or _facade().FRAMEWORK_MANIFEST.app_key)
    with _facade().SessionLocal() as db:
        catalog = {row.code: row for row in db.query(_facade().PermissionCatalog).all()}
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
        "app": _manifest_app(configured_app_key),
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
        "webhook": {"signing": "hmac-sha256", "events_url": "/api/v1/easyauth/events"},
    }


def _validate_descriptor_token(token: str | None) -> bool:
    with _facade().SessionLocal() as db:
        active = db.query(_facade().DescriptorKey).filter(_facade().DescriptorKey.active.is_(True)).all()
        if not active:
            return _facade().os.getenv("BLANK_RUNTIME_ENV", "production").strip().lower() != "production"
        if not token:
            return False
        token_hash = _facade().hashlib.sha256(token.encode()).hexdigest()
        matched = next((key for key in active if _facade().secrets.compare_digest(key.token_hash, token_hash)), None)
        if matched is None:
            return False
        matched.last_used_at = _facade().datetime.now(_facade().UTC)
        db.commit()
        return True


@descriptor_router.get("/.well-known/easyauth-app.json", include_in_schema=False)
def easyauth_descriptor(request: Request) -> JSONResponse:
    authorization = request.headers.get("authorization")
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :].strip() or None
    if not _facade()._validate_descriptor_token(token):
        return _facade().JSONResponse(
            status_code=401,
            content={"error": {"code": "descriptor_unauthorized", "message": "描述符访问未授权。"}},
        )
    manifest_payload = _facade()._current_manifest()
    return _facade().JSONResponse(
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
    with _facade().SessionLocal() as db:
        return db.query(_facade().DescriptorKey).order_by(_facade().DescriptorKey.created_at.desc()).all()


def _create_descriptor_key(payload: DescriptorKeyCreateRequest, *, actor_id: str) -> DescriptorKeyCreateResponse:
    name = payload.name.strip()
    if not name:
        raise _facade().HTTPException(422, "密钥名称不能为空")
    token = f"epd_{_facade().secrets.token_urlsafe(32)}"
    with _facade().SessionLocal() as db:
        row = _facade().DescriptorKey(
            name=name,
            token_prefix=token[:10],
            token_hash=_facade().hashlib.sha256(token.encode()).hexdigest(),
            active=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        response = _facade().DescriptorKeyResponse.model_validate(row)
    _facade().record_platform_audit(actor_id, "authz.descriptor_key.create", None, {"id": response.id, "name": name})
    return _facade().DescriptorKeyCreateResponse(key=response, token=token)


def _update_descriptor_key(key_id: uuid.UUID, payload: DescriptorKeyUpdateRequest, *, actor_id: str) -> DescriptorKey:
    with _facade().SessionLocal() as db:
        row = db.get(_facade().DescriptorKey, key_id)
        if row is None:
            raise _facade().HTTPException(404, "描述符同步密钥不存在")
        before = {"active": row.active}
        row.active = payload.active
        db.commit()
        db.refresh(row)
        db.expunge(row)
    _facade().record_platform_audit(actor_id, "authz.descriptor_key.update", before, {"active": payload.active})
    return row


def _delete_descriptor_key(key_id: uuid.UUID, *, actor_id: str) -> None:
    with _facade().SessionLocal() as db:
        row = db.get(_facade().DescriptorKey, key_id)
        if row is None:
            raise _facade().HTTPException(404, "描述符同步密钥不存在")
        before = {"name": row.name, "tokenPrefix": row.token_prefix, "active": row.active}
        db.delete(row)
        db.commit()
    _facade().record_platform_audit(actor_id, "authz.descriptor_key.delete", before, None)

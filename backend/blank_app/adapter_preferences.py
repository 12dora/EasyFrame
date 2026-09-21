"""blank host AccountPort methods for per-account UI preferences."""

from __future__ import annotations

from typing import Any

from blank_app.database import SessionLocal
from blank_app.models import Account
from enterprise_platform.auth import AuthError

_PREFERENCE_KEYS = ("table_density", "row_spacing")
_PREFERENCE_VALUES = frozenset({"compact", "comfortable"})


class UiPreferencesMixin:
    def get_ui_preferences(self, account_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            account = db.get(Account, account_id)
            if account is None:
                return {}
            raw = account.ui_preferences
            return dict(raw) if isinstance(raw, dict) else {}

    def update_ui_preferences(self, account_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with SessionLocal() as db:
            account = db.get(Account, account_id, with_for_update=True)
            if account is None:
                raise AuthError(401, "登录态无效")
            prefs = dict(account.ui_preferences) if isinstance(account.ui_preferences, dict) else {}
            # 只写补丁里带来的那几档:两档偏好各存各的,改一档不会把另一档抹掉。
            for key in _PREFERENCE_KEYS:
                value = patch.get(key)
                if value in _PREFERENCE_VALUES:
                    prefs[key] = value
            account.ui_preferences = prefs
            db.commit()
            return prefs

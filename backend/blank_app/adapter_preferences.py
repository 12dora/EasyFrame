"""blank host AccountPort methods for per-account UI preferences."""

from __future__ import annotations

from typing import Any

from blank_app.database import SessionLocal
from blank_app.models import Account
from enterprise_platform.auth import AuthError

_TABLE_DENSITY_KEY = "table_density"
_TABLE_DENSITY_VALUES = frozenset({"compact", "comfortable"})


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
            account = db.get(Account, account_id)
            if account is None:
                raise AuthError(401, "登录态无效")
            prefs = dict(account.ui_preferences) if isinstance(account.ui_preferences, dict) else {}
            density = patch.get(_TABLE_DENSITY_KEY)
            if density in _TABLE_DENSITY_VALUES:
                prefs[_TABLE_DENSITY_KEY] = density
            account.ui_preferences = prefs
            db.commit()
            return prefs

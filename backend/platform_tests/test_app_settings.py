"""GET/PUT /app-settings/general and the footer compatibility shim."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from enterprise_platform.footer import clean_plain_text, validate_logo_data_url
from platform_tests.test_blank_app_api import _login, _reset_admin

pytestmark = pytest.mark.usefixtures("blank_admin_seeded")

_GENERAL = "/api/v1/app-settings/general"
_FOOTER = "/api/v1/app-settings/footer"
_PNG_PREFIX = b"\x89PNG\r\n\x1a\n"


def _delete_settings(*keys: str) -> None:
    from blank_app.database import SessionLocal
    from blank_app.models import PlatformSetting

    with SessionLocal() as db:
        for key in keys:
            row = db.get(PlatformSetting, key)
            if row is not None:
                db.delete(row)
        db.commit()


@pytest.fixture(autouse=True)
def _clean_app_settings() -> None:
    _delete_settings("general", "footer")
    yield
    _delete_settings("general", "footer")


def _admin_headers(client: TestClient) -> dict[str, str]:
    _reset_admin()
    login = _login(client)
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['accessToken']}"}


def _png_data_url(*, extra_bytes: int = 16) -> str:
    return "data:image/png;base64," + base64.b64encode(_PNG_PREFIX + b"\x00" * extra_bytes).decode()


def _general_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "titleZh": "",
        "titleEn": "",
        "subtitleZh": "",
        "subtitleEn": "",
        "footerHtmlZh": "企业应用 · © {year}",
        "footerHtmlEn": "Enterprise App · © {year}",
        "logoDataUrl": None,
    }
    body.update(overrides)
    return body


def test_clean_plain_text_strips_and_rejects_markup() -> None:
    assert clean_plain_text("  Hello  ", max_length=80) == "Hello"
    with pytest.raises(ValueError, match="尖括号"):
        clean_plain_text("<b>x</b>", max_length=80)
    with pytest.raises(ValueError, match="过长"):
        clean_plain_text("x" * 9, max_length=8)


def test_logo_data_url_accepts_png_and_clears_empty() -> None:
    assert validate_logo_data_url(_png_data_url()).startswith("data:image/png;base64,")
    assert validate_logo_data_url(None) is None
    assert validate_logo_data_url("  ") is None


def test_logo_data_url_rejects_svg_javascript_mismatch_and_oversize() -> None:
    jpeg = b"\xff\xd8\xff" + b"\x00" * 16
    oversized = _PNG_PREFIX + b"\x00" * (128 * 1024)
    with pytest.raises(ValueError, match="无效"):
        validate_logo_data_url("javascript:alert(1)")
    with pytest.raises(ValueError, match="无效"):
        validate_logo_data_url("data:image/svg+xml;base64,PHN2Zz4=")
    with pytest.raises(ValueError, match="不符"):
        validate_logo_data_url("data:image/png;base64," + base64.b64encode(jpeg).decode())
    with pytest.raises(ValueError, match="过大"):
        validate_logo_data_url("data:image/png;base64," + base64.b64encode(oversized).decode())


def test_unauthenticated_get_general_returns_defaults() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        response = client.get(_GENERAL)
        assert response.status_code == 200
        assert response.json() == {
            "titleZh": "",
            "titleEn": "",
            "subtitleZh": "",
            "subtitleEn": "",
            "footerHtmlZh": "企业应用 · © {year}",
            "footerHtmlEn": "Enterprise App · © {year}",
            "logoDataUrl": None,
        }


def test_get_footer_shim_projects_general() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _admin_headers(client)
        saved = client.put(
            _GENERAL,
            headers=headers,
            json=_general_body(footerHtmlZh="中文页脚", footerHtmlEn="English footer"),
        )
        assert saved.status_code == 200, saved.text
        shim = client.get(_FOOTER)
        assert shim.status_code == 200
        assert shim.json() == {"footerHtmlZh": "中文页脚", "footerHtmlEn": "English footer"}


def test_put_general_requires_auth_and_permission() -> None:
    from blank_app.adapters import pwd_context
    from blank_app.database import SessionLocal
    from blank_app.main import app
    from blank_app.models import Account

    with TestClient(app) as client:
        assert client.put(_GENERAL, json=_general_body()).status_code == 401
        with SessionLocal() as db:
            if db.query(Account).filter(Account.username == "settings-viewer").one_or_none() is None:
                db.add(
                    Account(
                        username="settings-viewer",
                        email="viewer@example.com",
                        password_hash=pwd_context.hash("viewer-password-43!"),
                        active=True,
                        is_admin=False,
                        must_change_password=False,
                    )
                )
                db.commit()
        limited = client.post(
            "/api/v1/auth/login",
            json={"username": "settings-viewer", "password": "viewer-password-43!"},
        )
        assert limited.status_code == 200, limited.text
        denied = client.put(
            _GENERAL,
            headers={"Authorization": f"Bearer {limited.json()['accessToken']}"},
            json=_general_body(),
        )
        assert denied.status_code == 403


def test_put_general_persists_and_footer_shim_reflects() -> None:
    from blank_app.main import app

    logo = _png_data_url()
    with TestClient(app) as client:
        headers = _admin_headers(client)
        saved = client.put(
            _GENERAL,
            headers=headers,
            json=_general_body(titleZh="框架", titleEn="Frame", logoDataUrl=logo, footerHtmlEn="Saved footer"),
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["titleZh"] == "框架"
        assert body["titleEn"] == "Frame"
        assert body["logoDataUrl"] == logo
        listed = client.get(_GENERAL)
        assert listed.json()["titleZh"] == "框架"
        assert listed.json()["logoDataUrl"] == logo
        assert client.get(_FOOTER).json()["footerHtmlEn"] == "Saved footer"


def test_get_general_falls_back_to_legacy_footer_row() -> None:
    from blank_app.database import SessionLocal
    from blank_app.main import app
    from blank_app.models import PlatformSetting

    with SessionLocal() as db:
        db.add(PlatformSetting(key="footer", value={"footer_html_zh": "旧中文", "footer_html_en": "legacy en"}))
        db.commit()
    with TestClient(app) as client:
        response = client.get(_GENERAL)
        assert response.status_code == 200
        assert response.json()["footerHtmlZh"] == "旧中文"
        assert response.json()["footerHtmlEn"] == "legacy en"
        assert response.json()["titleZh"] == ""
        assert response.json()["logoDataUrl"] is None


@pytest.mark.parametrize(
    "logo",
    [
        "javascript:alert(1)",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64," + base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 16).decode(),
        "data:image/png;base64," + base64.b64encode(_PNG_PREFIX + b"\x00" * (128 * 1024)).decode(),
    ],
)
def test_put_general_rejects_invalid_logo(logo: str) -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _admin_headers(client)
        response = client.put(_GENERAL, headers=headers, json=_general_body(logoDataUrl=logo))
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)


def test_put_general_rejects_markup_in_title() -> None:
    from blank_app.main import app

    with TestClient(app) as client:
        headers = _admin_headers(client)
        response = client.put(_GENERAL, headers=headers, json=_general_body(titleZh="<b>x</b>"))
        assert response.status_code == 422
        assert "尖括号" in response.json()["detail"]


def test_put_footer_shim_leaves_title_and_logo_untouched() -> None:
    from blank_app.main import app

    logo = _png_data_url()
    with TestClient(app) as client:
        headers = _admin_headers(client)
        saved = client.put(
            _GENERAL,
            headers=headers,
            json=_general_body(titleZh="保留", titleEn="Keep", logoDataUrl=logo, footerHtmlEn="before"),
        )
        assert saved.status_code == 200, saved.text
        shim = client.put(_FOOTER, headers=headers, json={"footerHtmlZh": "新页脚", "footerHtmlEn": "new footer"})
        assert shim.status_code == 200, shim.text
        assert shim.json() == {"footerHtmlZh": "新页脚", "footerHtmlEn": "new footer"}
        general = client.get(_GENERAL).json()
        assert general["titleZh"] == "保留"
        assert general["titleEn"] == "Keep"
        assert general["logoDataUrl"] == logo
        assert general["footerHtmlEn"] == "new footer"

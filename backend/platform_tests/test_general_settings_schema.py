"""GeneralSettings.show_footer 缺省、落库形状与部分更新合并。"""

from enterprise_platform.assembly.footer_notification_routes import _general_from_update
from enterprise_platform.schemas import GeneralSettings, GeneralSettingsUpdate


def _update(**overrides: object) -> GeneralSettingsUpdate:
    payload: dict[str, object] = {
        "titleZh": "",
        "titleEn": "",
        "subtitleZh": "",
        "subtitleEn": "",
        "footerHtmlZh": "zh",
        "footerHtmlEn": "en",
    }
    payload.update(overrides)
    return GeneralSettingsUpdate.model_validate(payload)


def test_show_footer_defaults_true_when_missing_from_storage() -> None:
    settings = GeneralSettings(footer_html_zh="zh", footer_html_en="en")
    assert settings.show_footer is True
    parsed = GeneralSettings.model_validate({"footer_html_zh": "zh", "footer_html_en": "en"})
    assert parsed.show_footer is True
    assert settings.model_dump(mode="json", by_alias=True)["showFooter"] is True


def test_show_footer_round_trips_false() -> None:
    settings = GeneralSettings(footer_html_zh="zh", footer_html_en="en", show_footer=False)
    dumped = settings.model_dump(mode="json")
    assert dumped["show_footer"] is False
    assert GeneralSettings.model_validate(dumped).show_footer is False
    assert settings.model_dump(mode="json", by_alias=True)["showFooter"] is False


def test_update_omitting_show_footer_preserves_stored_false() -> None:
    current = GeneralSettings(footer_html_zh="old-zh", footer_html_en="old-en", show_footer=False)
    body = _update()
    assert body.show_footer is None
    assert _general_from_update(body, current).show_footer is False


def test_update_explicit_show_footer_overrides_current() -> None:
    current = GeneralSettings(footer_html_zh="old-zh", footer_html_en="old-en", show_footer=True)
    assert _general_from_update(_update(showFooter=False), current).show_footer is False
    assert _general_from_update(_update(showFooter=False)).show_footer is False

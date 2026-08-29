"""Tests for translation wrappers, locale selection, and formatting helpers."""

from __future__ import annotations

import gettext
import locale
from typing import TYPE_CHECKING

from FlashGBX import i18n

if TYPE_CHECKING:
    import pytest


class FakeTranslations:
    def gettext(self, message: str) -> str:
        return {"hello": "Hallo", "hello {name}": "Hallo {name}", "raw": "{missing}"}.get(message, message)

    def ngettext(self, singular: str, plural: str, number: int) -> str:
        return singular if number == 1 else plural

    def pgettext(self, _context: str, message: str) -> str:
        return f"context:{message}"

    def npgettext(self, _context: str, singular: str, plural: str, number: int) -> str:
        return singular if number == 1 else plural


def test_translation_wrappers_format_plural_and_contextual_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(i18n, "lang", FakeTranslations())

    assert i18n.__("hello") == "Hallo"
    assert i18n.__("hello {name}", name="Ada") == "Hallo Ada"
    assert i18n.___("{n} file", "{n} files", n=1) == "1 file"
    assert i18n.___("{n} file", "{n} files", n=2) == "2 files"
    assert i18n.c__("Context", "hello") == "context:hello"
    assert i18n.c___("Context", "{n} file", "{n} files", n=2) == "2 files"
    assert i18n.__("raw") == "raw"
    assert i18n.format_decimal(1.25, precision=2, localized=False) == "1.25"
    assert i18n.format_number(1234) == "1234"


def test_set_locale_tries_fallback_candidates_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def select_locale(_category: int, value: str) -> str:
        calls.append(value)
        if value == "en":
            return value
        raise locale.Error

    monkeypatch.setattr(i18n.locale, "setlocale", select_locale)
    assert i18n.set_locale("en_US.UTF-8") is True
    assert calls == ["en_US.UTF-8", "en_US", "en"]

    monkeypatch.setattr(i18n.locale, "setlocale", lambda *_args: (_ for _ in ()).throw(locale.Error))
    assert i18n.set_locale("xx_YY") is False


def test_load_translation_and_init_language_write_configuration(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translation = i18n.loadTranslation("de")
    assert isinstance(translation, gettext.GNUTranslations)

    monkeypatch.setattr(i18n, "lang", i18n.lang)
    monkeypatch.setattr(i18n, "CONFIGURED_LANGUAGE", i18n.CONFIGURED_LANGUAGE)
    monkeypatch.setattr(i18n, "TRANSLATION_AUTHOR", i18n.TRANSLATION_AUTHOR)
    monkeypatch.setattr(i18n, "set_locale", lambda _language: True)
    monkeypatch.setattr(i18n, "LANGUAGES", dict(i18n.LANGUAGES))
    i18n.init_language(tmp_path, override="de")  # type: ignore[arg-type]

    assert i18n.CONFIGURED_LANGUAGE == "de"
    assert "de" in i18n.LANGUAGES
    assert (tmp_path / "settings.ini").is_file()  # type: ignore[operator]

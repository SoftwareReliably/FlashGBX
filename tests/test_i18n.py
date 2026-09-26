"""Tests for translation wrappers, locale selection, and formatting helpers."""

from __future__ import annotations

import gettext
import locale
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import pytest

from FlashGBX import i18n

if TYPE_CHECKING:
    from pathlib import Path


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
    monkeypatch.setattr(i18n, "configured_language", i18n.configured_language)
    monkeypatch.setattr(i18n, "translation_author", i18n.translation_author)
    monkeypatch.setattr(i18n, "set_locale", lambda _language: True)
    monkeypatch.setattr(i18n, "LANGUAGES", dict(i18n.LANGUAGES))
    i18n.init_language(tmp_path, override="de")  # type: ignore[arg-type]

    assert i18n.configured_language == "de"
    assert "de" in i18n.LANGUAGES
    assert (tmp_path / "settings.ini").is_file()  # type: ignore[operator]


def test_load_translation_parses_context_and_plural_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locale_path = tmp_path / "locale"
    locale_path.mkdir()
    (locale_path / "xy.po").write_text(
        'msgid ""\n'
        'msgstr ""\n'
        '"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n'
        "# Context entry\n"
        'msgctxt "button"\n'
        'msgid "Open"\n'
        'msgstr "Öffnen"\n'
        "# Plain entries\n"
        'msgid "Close"\n'
        'msgstr "Schließen"\n'
        'msgid "apple"\n'
        'msgid_plural "apples"\n'
        'msgstr[0] "Apfel"\n'
        'msgstr[1] "Äpfel"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n, "_application_path", lambda: tmp_path)

    translation = i18n.loadTranslation("xy")

    assert translation.pgettext("button", "Open") == "Öffnen"
    assert translation.gettext("Close") == "Schließen"
    assert translation.ngettext("apple", "apples", 1) == "Apfel"
    assert translation.ngettext("apple", "apples", 2) == "Äpfel"


@pytest.mark.parametrize(
    ("source", "exception", "message"),
    [
        ('msgid "apple"\nmsgstr[0] "Apfel"\n', ValueError, "without msgid_plural"),
        (
            'msgid "apple"\nmsgid_plural "apples"\nmsgstr "Apfel"\n',
            ValueError,
            "Non-indexed msgstr",
        ),
        ("msgid 7\n", TypeError, "Expected a quoted string"),
    ],
)
def test_load_translation_rejects_invalid_po_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    exception: type[Exception],
    message: str,
) -> None:
    locale_path = tmp_path / "locale"
    locale_path.mkdir()
    (locale_path / "invalid.po").write_text(source, encoding="utf-8")
    monkeypatch.setattr(i18n, "_application_path", lambda: tmp_path)

    with pytest.raises(exception, match=message):
        i18n.loadTranslation("invalid")


def test_translation_wrappers_fall_back_for_legacy_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    format_calls: list[tuple[str, object, bool]] = []

    class LegacyTranslations:
        def gettext(self, _message: str) -> str:
            return "{missing}"

        def ngettext(self, _singular: str, _plural: str, _number: int) -> str:
            return "{missing}"

    def format_string(pattern: str, value: object, *, grouping: bool) -> str:
        format_calls.append((pattern, value, grouping))
        return "localized"

    monkeypatch.setattr(i18n, "lang", LegacyTranslations())
    monkeypatch.setattr(i18n.locale, "format_string", format_string)

    assert i18n.___("{n} file", "{n} files", n=1) == "1 file"
    assert i18n.___("{n} file", "{n} files", n=2) == "2 files"
    assert i18n.c__("button", "Hello {name}", name="Ada") == "Hello Ada"
    assert i18n.c___("count", "{n} item", "{n} items", n=2) == "2 items"
    assert i18n.format_decimal(1.25, precision=1, grouping=True) == "localized"
    assert format_calls == [("%.1f", 1.25, True)]


def test_load_qt_translation_uses_fallback_catalog_and_installs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    locale_names: list[str] = []

    class FakeLocale:
        selected: ClassVar[list[str]] = []

        def __init__(self, language: str) -> None:
            if language == "broken":
                raise ValueError(language)
            self.language = language

        @classmethod
        def system(cls) -> FakeLocale:
            return cls("system")

        @classmethod
        def setDefault(cls, qt_locale: FakeLocale) -> None:
            cls.selected.append(qt_locale.name())

        def name(self) -> str:
            return self.language

    class FakeLibraryInfo:
        class LibraryPath:
            TranslationsPath = object()

        @staticmethod
        def path(_path_type: object) -> str:
            return "/qt/translations"

    class FakeTranslator:
        attempts: ClassVar[list[str]] = []

        def load(self, _locale: FakeLocale, catalog: str, _separator: str, _path: str) -> bool:
            self.attempts.append(catalog)
            return catalog == "qt"

    qt_core = SimpleNamespace(QLocale=FakeLocale, QLibraryInfo=FakeLibraryInfo, QTranslator=FakeTranslator)
    monkeypatch.setitem(sys.modules, "PySide6", SimpleNamespace(QtCore=qt_core))
    monkeypatch.setattr(i18n, "set_locale", lambda language: locale_names.append(language) or True)

    def install_translator(translator: object) -> None:
        installed.append(translator)

    app = SimpleNamespace(installTranslator=install_translator)

    assert i18n.loadQtTranslation(app, "de_DE") is True
    assert FakeTranslator.attempts == ["qtbase", "qt"]
    assert FakeLocale.selected == ["de_DE"]
    assert locale_names == ["de_DE"]
    assert installed == [i18n._qt_translator]

    def fail_to_load(*_args: object) -> bool:
        return False

    FakeTranslator.load = fail_to_load  # type: ignore[method-assign]
    assert i18n.loadQtTranslation(language="broken") is False

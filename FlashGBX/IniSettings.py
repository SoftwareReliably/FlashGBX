# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import configparser
from io import StringIO
from pathlib import Path

from .i18n import __
from .Logging import dprint


class _SettingsParser(configparser.RawConfigParser):
    """Preserve setting names after the initial INI-string normalization."""

    preserve_case: bool = True

    def optionxform(self, optionstr: str) -> str:
        return optionstr if self.preserve_case else super().optionxform(optionstr)


class IniSettings:
    filename: Path | None = None
    settings: _SettingsParser | None = None
    main_section = "General"

    def __init__(self, path: str | Path = "", ini: str = "", main_section: str = "General") -> None:
        if path != "":
            settings_path = Path(path)
            try:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.touch(exist_ok=True)
            except Exception:
                print(__("Can't access the configuration directory or settings file."))
                return
            self.filename = settings_path
            self.settings = _SettingsParser()
            try:
                self.reload()
            except configparser.MissingSectionHeaderError:
                print(__("Resetting invalid settings file..."))
                settings_path.write_text("", encoding="UTF-8")
                path = ""

        if path == "":
            self.filename = None
            self.settings = _SettingsParser()
            self.settings.preserve_case = False
            self.settings.read_string(ini)
            self.settings.preserve_case = True

        self.main_section: str = main_section

    def reload(self) -> None:
        if self.settings is None:
            return
        if self.filename is not None:
            with self.filename.open(encoding="UTF-8") as f:
                self.settings.read_file(f)
        if not self.settings.has_section(self.main_section):
            self.settings.add_section(self.main_section)

    def value(self, key: str, default: str | None = None) -> str | None:
        if self.settings is None:
            return None
        self.reload()
        if key not in self.settings[self.main_section]:
            if default is not None:
                self.setValue(key, default)
            return default
        return self.settings[self.main_section][key]

    def setValue(self, key: str, value: str | None, quiet: bool = False) -> None:
        if self.settings is None:
            return
        self.reload()
        if value is None:
            if key in self.settings[self.main_section]:
                del self.settings[self.main_section][key]
        else:
            self.settings[self.main_section][key] = value
        if not quiet:
            dprint("Updating settings:", key, "=", value)
        if self.filename is not None:
            with self.filename.open("w", encoding="UTF-8") as f:
                self.settings.write(f)

    def clear(self) -> None:
        if self.settings is None:
            return
        self.settings.clear()
        if self.filename is not None:
            with self.filename.open("w", encoding="UTF-8") as f:
                self.settings.write(f)

    def get_string(self) -> str:
        if self.settings is None:
            return ""
        output = StringIO()
        self.settings.write(output)
        return output.getvalue()

    # Legacy PascalCase aliases — drop in a follow-up consumer sweep.
    def Reload(self) -> None:
        return self.reload()

    def GetValue(self, key: str, default: str | None = None) -> str | None:
        return self.value(key, default)

    def SetValue(self, key: str, value: str | None, quiet: bool = False) -> None:
        return self.setValue(key, value, quiet)

    def Clear(self) -> None:
        return self.clear()

    def GetString(self) -> str:
        return self.get_string()

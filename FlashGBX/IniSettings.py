# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import configparser
from io import StringIO
from pathlib import Path

from .i18n import __
from .Logging import dprint


class IniSettings:
    FILENAME: Path | None = None
    SETTINGS: configparser.RawConfigParser | None = None
    MAIN_SECTION = "General"

    def __init__(self, path: str | Path = "", ini: str = "", main_section: str = "General") -> None:
        if path != "":
            settings_path = Path(path)
            try:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.touch(exist_ok=True)
            except Exception:
                print(__("Can't access the configuration directory or settings file."))
                return
            self.FILENAME = settings_path
            self.SETTINGS = configparser.RawConfigParser()
            # ConfigParser supports an instance-level option-name transform.
            self.SETTINGS.optionxform = lambda optionstr: optionstr  # ty: ignore[invalid-assignment]
            try:
                self.reload()
            except configparser.MissingSectionHeaderError:
                print(__("Resetting invalid settings file..."))
                settings_path.write_text("", encoding="UTF-8")
                path = ""

        if path == "":
            self.FILENAME = None
            self.SETTINGS = configparser.RawConfigParser()
            self.SETTINGS.read_string(ini)
            # ConfigParser supports an instance-level option-name transform.
            self.SETTINGS.optionxform = lambda optionstr: optionstr  # ty: ignore[invalid-assignment]

        self.MAIN_SECTION: str = main_section

    def reload(self) -> None:
        if self.SETTINGS is None:
            return
        if self.FILENAME is not None:
            with self.FILENAME.open(encoding="UTF-8") as f:
                self.SETTINGS.read_file(f)
        if not self.SETTINGS.has_section(self.MAIN_SECTION):
            self.SETTINGS.add_section(self.MAIN_SECTION)

    def value(self, key: str, default: str | None = None) -> str | None:
        if self.SETTINGS is None:
            return None
        self.reload()
        if key not in self.SETTINGS[self.MAIN_SECTION]:
            if default is not None:
                self.setValue(key, default)
            return default
        return self.SETTINGS[self.MAIN_SECTION][key]

    def setValue(self, key: str, value: str | None, quiet: bool = False) -> None:
        if self.SETTINGS is None:
            return
        self.reload()
        if value is None:
            if key in self.SETTINGS[self.MAIN_SECTION]:
                del self.SETTINGS[self.MAIN_SECTION][key]
        else:
            self.SETTINGS[self.MAIN_SECTION][key] = value
        if not quiet:
            dprint("Updating settings:", key, "=", value)
        if self.FILENAME is not None:
            with self.FILENAME.open("w", encoding="UTF-8") as f:
                self.SETTINGS.write(f)

    def clear(self) -> None:
        if self.SETTINGS is None:
            return
        self.SETTINGS.clear()
        if self.FILENAME is not None:
            with self.FILENAME.open("w", encoding="UTF-8") as f:
                self.SETTINGS.write(f)

    def get_string(self) -> str:
        if self.SETTINGS is None:
            return ""
        output = StringIO()
        self.SETTINGS.write(output)
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

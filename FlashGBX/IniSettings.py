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

    def __init__(self, path: str | Path = "", ini: str = "", main_section: str = "General"):
        if path != "":
            settings_path = Path(path)
            try:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.touch(exist_ok=True)
            except Exception:
                print(__("Can’t access the configuration directory or settings file."))
                return
            self.FILENAME = settings_path
            self.SETTINGS = configparser.RawConfigParser()
            self.SETTINGS.optionxform = lambda optionstr: optionstr
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
            self.SETTINGS.optionxform = lambda optionstr: optionstr

        self.MAIN_SECTION = main_section

    def reload(self):
        if self.SETTINGS is None:
            return
        if self.FILENAME is not None:
            with self.FILENAME.open(encoding="UTF-8") as f:
                self.SETTINGS.read_file(f)
        if not self.SETTINGS.has_section(self.MAIN_SECTION):
            self.SETTINGS.add_section(self.MAIN_SECTION)

    def value(self, key, default=None):
        if self.SETTINGS is None:
            return None
        self.reload()
        if key not in self.SETTINGS[self.MAIN_SECTION]:
            if default is not None:
                self.setValue(key, default)
            return default
        return self.SETTINGS[self.MAIN_SECTION][key]

    def setValue(self, key, value, quiet=False):
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

    def clear(self):
        if self.SETTINGS is None:
            return
        self.SETTINGS.clear()
        if self.FILENAME is not None:
            with self.FILENAME.open("w", encoding="UTF-8") as f:
                self.SETTINGS.write(f)

    def get_string(self):
        if self.SETTINGS is None:
            return ""
        output = StringIO()
        self.SETTINGS.write(output)
        return output.getvalue()

    # Legacy PascalCase aliases — drop in a follow-up consumer sweep.
    def Reload(self):
        return self.reload()

    def GetValue(self, key, default=None):
        return self.value(key, default)

    def SetValue(self, key, value, quiet=False):
        return self.setValue(key, value, quiet)

    def Clear(self):
        return self.clear()

    def GetString(self):
        return self.get_string()

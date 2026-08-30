# FlashGBX
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import importlib
import platform
import re
import sys
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, cast

from loguru import logger  # pyright: ignore[reportMissingImports]

if TYPE_CHECKING:
    from types import ModuleType, TracebackType


class _WindowsVersion(Protocol):
    major: int
    minor: int
    build: int


class _RegistryKey(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class _RegistryModule(Protocol):
    HKEY_LOCAL_MACHINE: object

    def OpenKey(self, key: object, sub_key: str) -> _RegistryKey: ...

    def QueryValueEx(self, key: object, value_name: str) -> tuple[object, int]: ...


class SettingsReader(Protocol):
    def value(self, key: str, default: str) -> object: ...


FilenameHeader = Mapping[str, object]
CartridgeMode = Literal["DMG", "AGB"]
_INVALID_FILENAME_CHARS = re.compile(r"[<>:\"/\\|\?\*]")


def _get_windows_version() -> _WindowsVersion:
    getter = getattr(sys, "getwindowsversion", None)
    if not callable(getter):
        msg = "sys.getwindowsversion is unavailable"
        raise OSError(msg)
    return cast("Callable[[], _WindowsVersion]", getter)()


def _required_text(values: Mapping[str, object], key: str) -> str:
    value = values[key]
    if not isinstance(value, str):
        msg = f"Header field {key!r} must be a string"
        raise TypeError(msg)
    return value


def _required_int(values: Mapping[str, object], key: str) -> int:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"Header field {key!r} must be an integer"
        raise TypeError(msg)
    return value


def _registry_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        msg = "Registry value must be an integer or an integer string"
        raise TypeError(msg)
    return int(value)


def _setting_text(settings: SettingsReader | None, key: str, default: str) -> str:
    if settings is None:
        return default
    value = settings.value(key=key, default=default)
    return value if isinstance(value, str) else default


def _gbmemory_cart_id(value: object) -> str | None:
    entry: Mapping[object, object]
    if isinstance(value, Mapping):
        entry = value
    elif isinstance(value, list) and value and isinstance(value[0], Mapping):
        entry = value[0]
    else:
        return None

    cart_id = entry.get("cart_id")
    return cart_id if isinstance(cart_id, str) and cart_id else None


def _database_filename(
    header: FilenameHeader,
    extension: str,
    *,
    gbmemory_cart_id: str | None,
) -> str | None:
    database = header.get("db")
    if database is None:
        return None
    if gbmemory_cart_id is not None:
        return f"NP GB-Memory Cartridge ({gbmemory_cart_id}).{extension}"
    if not isinstance(database, Mapping):
        msg = "Header field 'db' must be a mapping or None"
        raise TypeError(msg)
    game_name = _required_text(database, "gn")
    edition_name = _required_text(database, "ne")
    return f"{game_name} {edition_name}.{extension}"


class AppInfo:
    NAME: ClassVar[str] = "FlashGBX"
    VERSION_PEP440: ClassVar[str] = "5.0.1"
    VERSION: ClassVar[str] = f"v{VERSION_PEP440:s}"
    VERSION_TIMESTAMP: ClassVar[int] = 1780697375

    @classmethod
    def os_string(cls) -> str:
        if platform.system() != "Windows":
            return platform.platform()

        try:
            w = _get_windows_version()
            if w.major == 10 and w.build >= 22000:
                name = "Windows 11"
            elif w.major == 10:
                name = "Windows 10"
            elif w.major == 6 and w.minor == 3:
                name = "Windows 8.1"
            elif w.major == 6 and w.minor == 2:
                name = "Windows 8"
            elif w.major == 6 and w.minor == 1:
                name = "Windows 7"
            elif w.major == 6 and w.minor == 0:
                name = "Windows Vista"
            elif w.major == 5 and w.minor == 1:
                name = "Windows XP"
            else:
                name = f"Windows {w.major}.{w.minor}"

            display_version = None
            ubr = None
            try:
                winreg = cast("_RegistryModule", importlib.import_module("winreg"))

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                ) as key:
                    try:
                        product_name = winreg.QueryValueEx(key, "ProductName")[0]
                        # Keep build-based naming for 10/11 to avoid compatibility-masked ProductName values.
                        if w.major != 10 and isinstance(product_name, str) and product_name.startswith("Windows "):
                            parts = product_name.split(" ")
                            if len(parts) >= 2:
                                name = " ".join(parts[:2])
                    except Exception:
                        logger.exception("Failed to read the Windows product name")
                    try:
                        display_version = winreg.QueryValueEx(key, "DisplayVersion")[0]
                    except Exception as e:
                        try:
                            display_version = winreg.QueryValueEx(key, "ReleaseId")[0]
                        except Exception:
                            logger.exception("Failed to read the Windows release ID: {}", e)
                    try:
                        ubr = _registry_int(winreg.QueryValueEx(key, "UBR")[0])
                    except Exception:
                        logger.exception("Failed to read the Windows update build revision")
            except Exception:
                logger.exception("Failed to read Windows version details from the registry")

            build_str = f"{w.build}.{ubr}" if ubr is not None else f"{w.build}"
            if display_version:
                return f"{name} (Version {display_version}, Build {build_str})"
            return f"{name} (Build {build_str})"  # noqa: TRY300
        except Exception as e:
            logger.exception("Failed to determine Windows version: {}", e)
            release = platform.release()
            version = platform.version()
            if release:
                return f"Windows {release} ({version})"
            return platform.platform()


class AppContext:
    DEBUG: ClassVar[bool] = False
    APP_PATH: ClassVar[str] = ""
    CONFIG_PATH: ClassVar[str] = ""
    LAUNCH_TIMESTAMP: ClassVar[float] = 0.0
    DEBUG_LOG: ClassVar[list[str]] = []
    PRINT_LOG: ClassVar[list[str]] = []


def generate_filename(
    mode: CartridgeMode | None,
    header: FilenameHeader,
    settings: SettingsReader | None = None,
) -> str:
    from .Mapper import get_mbc_name

    use_no_intro_filename = _setting_text(settings, "UseNoIntroFilenames", "enabled").lower() == "enabled"

    path = "ROM"
    path_extension = "bin"
    gbmemory_cart_id: str | None = None

    if mode == "DMG":
        path_title = _required_text(header, "game_title")
        path_code = ""
        path_revision = str(header["version"])
        mapper_raw = _required_int(header, "mapper_raw")
        mapper_name = get_mbc_name(mapper_raw)
        if mapper_name == "G-MMC1":
            gbmemory_cart_id = _gbmemory_cart_id(header.get("gbmem_parsed"))
        path = "%TITLE%-%REVISION%"
        path = _setting_text(settings, "FileNameFormatDMG", path)
        auto_sgb_extension = _setting_text(settings, "AutoFileExtensionSGB", "enabled")

        game_code = _required_text(header, "game_code")
        if game_code:
            path_code = game_code
            path = "%TITLE%_%CODE%-%REVISION%"
            path = _setting_text(settings, "FileNameFormatCGB", path)

        if mapper_raw >= 0x200:
            path = "%TITLE%"
        if _required_int(header, "cgb") in (0xC0, 0x80):
            path_extension = "gbc"
        elif (
            _required_int(header, "old_lic") == 0x33
            and _required_int(header, "sgb") == 0x03
            and auto_sgb_extension.lower() == "enabled"
        ):
            path_extension = "sgb"
        else:
            path_extension = "gb"
        if path_title == "":
            path = f"ROM.{path_extension:s}"
        else:
            path = path.replace("%TITLE%", path_title.strip())
            path = path.replace("%CODE%", path_code.strip())
            path = path.replace("%REVISION%", path_revision)
            path = path.replace("%MAPPER%", mapper_name)
            path = _INVALID_FILENAME_CHARS.sub("_", path)
            if gbmemory_cart_id is not None:
                path += f"_{gbmemory_cart_id}"
            path += f".{path_extension:s}"
    elif mode == "AGB":
        path = "%TITLE%_%CODE%-%REVISION%"
        path = _setting_text(settings, "FileNameFormatAGB", path)
        path_title = _required_text(header, "game_title")
        path_code = _required_text(header, "game_code")
        path_revision = str(header["version"])
        path_extension = "gba"
        if path_title == "" and path_code == "":
            path = "ROM"
        else:
            path = path.replace("%TITLE%", path_title.strip())
            path = path.replace("%CODE%", path_code.strip())
            path = path.replace("%REVISION%", path_revision)
            path = _INVALID_FILENAME_CHARS.sub("_", path)
        path += "." + path_extension

    if use_no_intro_filename:
        database_path = _database_filename(
            header,
            path_extension,
            gbmemory_cart_id=gbmemory_cart_id,
        )
        if database_path is not None:
            path = database_path

    return path


# Hardware device backends
_hw_devices: list[ModuleType] = []
HW_DEVICE_MODULES: list[str] = ["hw_GBxCartRW", "hw_GBFlash", "hw_JoeyJr", "hw_GameBub"]
for _name in HW_DEVICE_MODULES:
    try:
        _hw_devices.append(importlib.import_module(f"{__package__}.{_name}"))
    except Exception:
        logger.exception("Failed to load hardware backend: {}", _name)
HW_DEVICES: list[ModuleType] = _hw_devices

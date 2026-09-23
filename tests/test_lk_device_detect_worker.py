"""Cartridge-detection worker scenarios with bounded hardware boundaries."""

from __future__ import annotations

from typing import Any

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice


def install_detection_worker_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "DMG",
    mapper_raw: int = 0x03,
    cart_type_id: int = 1,
    check_save_type: bool | None = None,
    save_read_result: bool = True,
    header_result: dict[str, Any] | bool | None = None,
    flash_result: tuple[Any, ...] | bool | None = None,
    prepare_error: Exception | None = None,
    info_extra: dict[str, Any] | None = None,
    firmware_version: int = 12,
    supports_power_cycle: bool = True,
    auto_poweroff_enabled: int = 1,
    skip_power_cycle: bool = False,
    save_data: bytearray | None = None,
) -> tuple[GbxDevice, dict[str, list[Any]], dict[str, Any], tuple[Any, ...]]:
    """Use the real worker and classifiers with recorded power and I/O calls."""
    device = GbxDevice()
    device.MODE = mode  # type: ignore[assignment]
    device.FW = {"fw_ver": firmware_version, "pcb_ver": 6}
    device.SKIP_POWERCYCLE = skip_power_cycle
    device.INFO["action"] = "active"
    device.INFO["last_action"] = 99
    info: dict[str, Any] = {
        "mapper_raw": mapper_raw,
        "3d_memory": False,
        "dacs_8m": False,
    }
    if info_extra is not None:
        info.update(info_extra)
    profile = {"name": "selected profile"}
    device.SUPPORTED_CARTS = {
        "DMG": {"retail": {"name": "retail profile"}, "selected": profile},
        "AGB": {"retail": {"name": "retail profile"}, "selected": profile},
    }
    flash_types = [cart_type_id]
    cfi = {"manufacturer": "test"}
    default_flash_result = (flash_types, cart_type_id, "12 34", "CFI summary", cfi, 0x10000)
    if flash_result is None:
        flash_result = default_flash_result
    save_payload = save_data if save_data is not None else bytearray([0x11] * 0x4000 + [0x22] * 0x4000)
    records: dict[str, list[Any]] = {
        "header_calls": [],
        "flash_calls": [],
        "preparations": [],
        "save_reads": [],
        "power_cycles": [],
        "firmware_reads": [],
        "firmware_writes": [],
        "device_writes": [],
        "progress": [],
        "flash_save_id_reads": [],
        "batteryless_checks": [],
    }

    def read_header(*, checkRtc: bool = True) -> dict[str, Any] | bool:
        records["header_calls"].append(checkRtc)
        return info if header_result is None else header_result

    def detect_flash(*, limitVoltage: bool = False) -> tuple[Any, ...] | bool:
        records["flash_calls"].append(limitVoltage)
        return flash_result

    def prepare(
        cart_type: dict[str, Any],
        should_check_save_type: bool,
        header: dict[str, Any],
        signal: object,
    ) -> tuple[bool, int | None, int | None]:
        records["preparations"].append((cart_type, should_check_save_type, header, signal))
        if prepare_error is not None:
            raise prepare_error
        if check_save_type is None:
            return should_check_save_type, None, None
        return check_save_type, None, None

    def backup_restore(*, args: dict[str, Any]) -> bool:
        records["save_reads"].append(args.copy())
        if save_read_result:
            device.INFO["data"] = bytearray(save_payload)
        return save_read_result

    def get_fw_variable(name: str) -> int:
        records["firmware_reads"].append(name)
        return {"AUTO_POWEROFF_ENABLED": auto_poweroff_enabled, "AUTO_POWEROFF_TIME": 30_000}[name]

    def set_fw_variable(name: str, value: int) -> None:
        records["firmware_writes"].append((name, value))

    def set_progress(event: dict[str, Any], signal: object = None) -> None:
        records["progress"].append((event.copy(), signal))

    monkeypatch.setattr(device, "ReadHeader", read_header)
    monkeypatch.setattr(device, "DetectFlash", detect_flash)
    monkeypatch.setattr(device, "_PrepareCartridgeSaveDetection", prepare)
    monkeypatch.setattr(device, "_BackupRestoreRAM", backup_restore)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: supports_power_cycle)
    monkeypatch.setattr(device, "CartPowerCycle", lambda: records["power_cycles"].append(True))
    monkeypatch.setattr(device, "_get_fw_variable", get_fw_variable)
    monkeypatch.setattr(device, "_set_fw_variable", set_fw_variable)
    monkeypatch.setattr(device, "_write", lambda value, wait=False: records["device_writes"].append((value, wait)))
    monkeypatch.setattr(device, "SetProgress", set_progress)
    monkeypatch.setattr(
        device,
        "ReadFlashSaveID",
        lambda: records["flash_save_id_reads"].append(True) or False,
    )
    monkeypatch.setattr(
        device,
        "CheckBatterylessSRAM",
        lambda: records["batteryless_checks"].append(True) or False,
    )
    return device, records, info, default_flash_result


def returned_flash_fields(flash_result: tuple[Any, ...]) -> tuple[Any, ...]:
    """Arrange DetectFlash fields in the worker's public result order."""
    return (
        flash_result[0],
        flash_result[1],
        flash_result[3],
        flash_result[4],
        flash_result[2],
        flash_result[5],
    )


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_detection_worker_success_returns_all_fields_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    device, records, info, flash_result = install_detection_worker_boundaries(monkeypatch, mode=mode)
    signal = object()
    args_mbc = 0x13

    result = device._DetectCartridge_Worker(
        mbc=args_mbc,
        limitVoltage=True,
        checkSaveType=True,
        signal=signal,  # type: ignore[arg-type]
    )

    save_type = 0x04 if mode == "DMG" else 8
    assert result == (
        info,
        0x8000,
        3,
        None,
        None,
        *returned_flash_fields(flash_result),
    )
    assert len(result) == 11  # type: ignore[arg-type]
    assert records["header_calls"] == [True]
    assert records["flash_calls"] == [True]
    selected_profile = device.SUPPORTED_CARTS[mode]["selected"]
    assert records["preparations"] == [(selected_profile, True, info, signal)]
    assert records["save_reads"] == [
        {
            "mode": 2,
            "path": None,
            "mbc": args_mbc,
            "save_type": save_type,
            "rtc": False,
            "detect": True,
        },
    ]
    assert records["progress"] == [
        ({"action": "UPDATE_INFO", "text": "Detecting ROM..."}, signal),
        ({"action": "UPDATE_INFO", "text": "Detecting Flash..."}, signal),
        ({"action": "UPDATE_INFO", "text": "Detecting save type..."}, signal),
    ]
    assert records["power_cycles"] == [True]
    assert records["firmware_reads"] == ["AUTO_POWEROFF_ENABLED", "AUTO_POWEROFF_TIME"]
    assert records["firmware_writes"] == [
        ("AUTO_POWEROFF_TIME", 5000),
        ("AUTO_POWEROFF_TIME", 30_000),
    ]
    assert records["device_writes"] == [(device.DEVICE_CMD["DMG_MBC_RESET"], True)]
    assert device.INFO["last_action"] == 0
    assert device.INFO["action"] is None


@pytest.mark.parametrize(
    ("case", "firmware_version", "supports_power_cycle", "auto_poweroff_enabled", "skip_power_cycle"),
    [
        ("old-firmware", 11, True, 1, False),
        ("unsupported-power-cycle", 12, False, 1, False),
        ("power-cycle-skipped", 12, True, 1, True),
        ("auto-poweroff-disabled", 12, True, 0, False),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_detection_worker_leaves_poweroff_unchanged_when_it_is_not_enabled_for_detection(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    firmware_version: int,
    supports_power_cycle: bool,
    auto_poweroff_enabled: int,
    skip_power_cycle: bool,
) -> None:
    del case
    device, records, _info, _flash_result = install_detection_worker_boundaries(
        monkeypatch,
        firmware_version=firmware_version,
        supports_power_cycle=supports_power_cycle,
        auto_poweroff_enabled=auto_poweroff_enabled,
        skip_power_cycle=skip_power_cycle,
    )

    device._DetectCartridge_Worker(checkSaveType=False)

    should_power_cycle = firmware_version >= 12 and supports_power_cycle and not skip_power_cycle
    assert records["power_cycles"] == ([True] if should_power_cycle else [])
    assert records["firmware_reads"] == (["AUTO_POWEROFF_ENABLED"] if should_power_cycle else [])
    expected_writes = [("AUTO_POWEROFF_TIME", 5000), ("AUTO_POWEROFF_TIME", 30_000)]
    assert records["firmware_writes"] == (expected_writes if should_power_cycle and auto_poweroff_enabled == 1 else [])


@pytest.mark.parametrize(
    ("case", "mapper_raw", "cart_type_id", "check_save_type"),
    [
        ("detection-disabled", 0x03, 1, False),
        ("retail-dmg-profile", 0x03, 0, True),
        ("high-mapper-id", 0x300, 1, True),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_detection_worker_skips_save_read_for_disabled_paths(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mapper_raw: int,
    cart_type_id: int,
    check_save_type: bool,
) -> None:
    del case
    device, records, info, flash_result = install_detection_worker_boundaries(
        monkeypatch,
        mapper_raw=mapper_raw,
        cart_type_id=cart_type_id,
    )

    result = device._DetectCartridge_Worker(checkSaveType=check_save_type)

    assert result == (info, None, None, None, None, *returned_flash_fields(flash_result))
    prepared_check = check_save_type and cart_type_id != 0 and mapper_raw <= 0x200
    assert records["preparations"][0][1] is prepared_check
    assert records["save_reads"] == []
    assert records["progress"] == []
    assert records["firmware_writes"] == [
        ("AUTO_POWEROFF_TIME", 5000),
        ("AUTO_POWEROFF_TIME", 30_000),
    ]
    assert records["device_writes"] == [(device.DEVICE_CMD["DMG_MBC_RESET"], True)]
    assert device.INFO["last_action"] == 0
    assert device.INFO["action"] is None


def test_detection_worker_skips_agb_save_probes_for_3d_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, records, info, flash_result = install_detection_worker_boundaries(
        monkeypatch,
        mode="AGB",
        info_extra={"3d_memory": True},
    )

    result = device._DetectCartridge_Worker(mbc=0x13, checkSaveType=True)

    assert result == (info, 0, None, None, None, *returned_flash_fields(flash_result))
    assert records["preparations"][0][1] is True
    assert records["save_reads"] == []
    assert records["flash_save_id_reads"] == []
    assert records["batteryless_checks"] == []


@pytest.mark.parametrize(
    ("mapper_raw", "expected_size", "expected_type"),
    [(0x20, 1_081_344, 0x104), (0xFD, 32, 0x103), (0x105, 0x20000, 0x04)],
    ids=["MBC6", "TAMA5", "G-MMC1"],
)
def test_detection_worker_special_mapper_returns_restore_poweroff_timeout(
    monkeypatch: pytest.MonkeyPatch,
    mapper_raw: int,
    expected_size: int,
    expected_type: int,
) -> None:
    device, records, info, flash_result = install_detection_worker_boundaries(
        monkeypatch,
        mapper_raw=mapper_raw,
    )

    result = device._DetectCartridge_Worker(checkSaveType=True)

    assert result == (info, expected_size, expected_type, None, None, *returned_flash_fields(flash_result))
    assert records["save_reads"] == []
    assert records["firmware_writes"] == [
        ("AUTO_POWEROFF_TIME", 5000),
        ("AUTO_POWEROFF_TIME", 30_000),
    ]


def test_detection_worker_uses_mbc7_save_type_without_early_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, records, info, flash_result = install_detection_worker_boundaries(
        monkeypatch,
        mapper_raw=0x22,
        save_data=bytearray([0x11] * 256 + [0x22] * 256),
    )

    result = device._DetectCartridge_Worker(checkSaveType=True)

    assert result == (info, 512, 0x102, None, None, *returned_flash_fields(flash_result))
    assert records["save_reads"][0]["save_type"] == 0x102
    assert records["device_writes"] == [(device.DEVICE_CMD["DMG_MBC_RESET"], True)]


@pytest.mark.parametrize("failure", ["header", "flash"])
def test_detection_worker_header_and_flash_failures_have_bounded_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    worker_args = {"header_result": False} if failure == "header" else {"flash_result": False}
    device, records, _info, _flash_result = install_detection_worker_boundaries(monkeypatch, **worker_args)

    assert device._DetectCartridge_Worker() is False
    assert records["power_cycles"] == ([] if failure == "header" else [True])
    assert records["flash_calls"] == ([] if failure == "header" else [False])
    assert records["preparations"] == []
    assert records["save_reads"] == []
    assert records["firmware_writes"] == (
        [] if failure == "header" else [("AUTO_POWEROFF_TIME", 5000), ("AUTO_POWEROFF_TIME", 30_000)]
    )


def test_detection_worker_failed_save_read_returns_empty_detection_and_restores_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, records, info, flash_result = install_detection_worker_boundaries(
        monkeypatch,
        save_read_result=False,
    )

    result = device._DetectCartridge_Worker(mbc=0x13, checkSaveType=True)

    assert result == (info, 0, 0, None, None, *returned_flash_fields(flash_result))
    assert len(records["save_reads"]) == 1
    assert records["firmware_writes"] == [
        ("AUTO_POWEROFF_TIME", 5000),
        ("AUTO_POWEROFF_TIME", 30_000),
    ]


def test_detection_worker_restores_timeout_when_a_collaborator_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("save preparation failed")
    device, records, _info, _flash_result = install_detection_worker_boundaries(
        monkeypatch,
        prepare_error=error,
    )

    with pytest.raises(RuntimeError, match="save preparation failed") as caught:
        device._DetectCartridge_Worker(mbc=0x13, checkSaveType=True)

    assert caught.value is error
    assert records["firmware_writes"] == [
        ("AUTO_POWEROFF_TIME", 5000),
        ("AUTO_POWEROFF_TIME", 30_000),
    ]
